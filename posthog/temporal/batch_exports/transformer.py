import abc
import gzip
import json
import typing
from io import BytesIO

import brotli
import orjson
import pyarrow as pa
import pyarrow.parquet as pq
import structlog

logger = structlog.get_logger()


def replace_broken_unicode(obj):
    if isinstance(obj, str):
        return obj.encode("utf-8", "replace").decode("utf-8")
    elif isinstance(obj, list):
        return [replace_broken_unicode(item) for item in obj]
    elif isinstance(obj, dict):
        return {replace_broken_unicode(key): replace_broken_unicode(value) for key, value in obj.items()}
    else:
        return obj


def json_dumps_bytes(d) -> bytes:
    try:
        return orjson.dumps(d, default=str)
    except orjson.JSONEncodeError:
        # orjson is very strict about invalid unicode. This slow path protects us against
        # things we've observed in practice, like single surrogate codes, e.g. "\ud83d"
        logger.exception("Failed to encode with orjson: %s", d)
        cleaned_d = replace_broken_unicode(d)
        return orjson.dumps(cleaned_d, default=str)


def get_stream_transformer(
    format: str, compression: str | None = None, schema: pa.Schema | None = None, include_inserted_at: bool = False
) -> "StreamTransformer":
    match format.lower():
        case "jsonlines":
            return JSONLStreamTransformer(compression=compression, include_inserted_at=include_inserted_at)
        case "parquet":
            if schema is None:
                raise ValueError("Schema is required for Parquet")
            return ParquetStreamTransformer(
                compression=compression, schema=schema, include_inserted_at=include_inserted_at
            )
        case _:
            raise ValueError(f"Unsupported format: {format}")


class StreamTransformer:
    """Transforms PyArrow RecordBatches to different formats with compression"""

    def __init__(self, compression: str | None = None, include_inserted_at: bool = False):
        self.compression = compression.lower() if compression else None
        self.include_inserted_at = include_inserted_at

        self._brotli_compressor = None

    def transform_batch(self, batch: pa.RecordBatch) -> typing.Generator[bytes, None, None]:
        """Transform a single batch and yield compressed bytes"""
        column_names = batch.column_names
        if not self.include_inserted_at:
            column_names.pop(column_names.index("_inserted_at"))

        yield from self.write_batch(batch.select(column_names))

    def finalize(self) -> typing.Generator[bytes, None, None]:
        """Finalize and yield any remaining data"""
        if self.compression == "brotli":
            yield self.brotli_compressor.finish()

    @abc.abstractmethod
    def write_batch(self, batch: pa.RecordBatch) -> typing.Generator[bytes, None, None]:
        """Write a batch to the output format"""
        raise NotImplementedError("Subclasses must implement write_batch")

    def compress(self, content: bytes | str) -> bytes:
        if isinstance(content, str):
            encoded = content.encode("utf-8")
        else:
            encoded = content

        match self.compression:
            case "gzip":
                return gzip.compress(encoded)
            case "brotli":
                self.brotli_compressor.process(encoded)
                return self.brotli_compressor.flush()
            case None:
                return encoded
            case _:
                raise ValueError(f"Unsupported compression: '{self.compression}'")

    @property
    def brotli_compressor(self):
        if self._brotli_compressor is None:
            self._brotli_compressor = brotli.Compressor()
        return self._brotli_compressor

    def finish_brotli_compressor(self) -> typing.Generator[bytes, None, None]:
        """Flush remaining brotli bytes."""
        if self.compression != "brotli":
            raise ValueError(f"Compression is '{self.compression}', not 'brotli'")

        yield self.brotli_compressor.finish()
        self._brotli_compressor = None


class JSONLStreamTransformer(StreamTransformer):
    def _write_dict(self, d: dict[str, typing.Any]) -> typing.Generator[bytes, None, None]:
        """Write a single row of JSONL."""
        try:
            data_gen = self._write(orjson.dumps(d, default=str) + b"\n")
        except orjson.JSONEncodeError as err:
            # NOTE: `orjson.JSONEncodeError` is actually just an alias for `TypeError`.
            # This handler will catch everything coming from orjson, so we have to
            # awkwardly check error messages.
            if str(err) == "Recursion limit reached":
                # Orjson enforces an unmodifiable recursion limit (256), so we can't
                # dump very nested dicts.
                if d.get("event", None) == "$web_vitals":
                    # These are PostHog events that for a while included a bunch of
                    # nested DOM structures. Eventually, this was removed, but these
                    # events could still be present in database.
                    # Let's try to clear the key with nested elements first.
                    try:
                        del d["properties"]["$web_vitals_INP_event"]["attribution"]["interactionTargetElement"]
                    except KeyError:
                        # We tried, fallback to the slower but more permissive stdlib
                        # json.
                        logger.exception("PostHog $web_vitals event didn't match expected structure")
                        dumped = json.dumps(d, default=str).encode("utf-8")
                        data_gen = self._write(dumped + b"\n")
                    else:
                        dumped = orjson.dumps(d, default=str)
                        data_gen = self._write(dumped + b"\n")

                else:
                    # In this case, we fallback to the slower but more permissive stdlib
                    # json.
                    logger.exception("Orjson detected a deeply nested dict: %s", d)
                    dumped = json.dumps(d, default=str).encode("utf-8")
                    data_gen = self._write(dumped + b"\n")
            else:
                # Orjson is very strict about invalid unicode. This slow path protects us
                # against things we've observed in practice, like single surrogate codes, e.g.
                # "\ud83d"
                logger.exception("Failed to encode with orjson: %s", d)
                cleaned_content = replace_broken_unicode(d)
                data_gen = self._write(orjson.dumps(cleaned_content, default=str) + b"\n")
        yield from data_gen

    def write_batch(self, record_batch: pa.RecordBatch) -> typing.Generator[bytes, None, None]:
        """Write records to a temporary file as JSONL."""
        for record_dict in record_batch.to_pylist():
            if not record_dict:
                continue

            yield from self._write_dict(record_dict)

    def _write(self, content: bytes | str):
        """Write bytes to underlying file keeping track of how many bytes were written."""
        compressed_content = self.compress(content)
        yield compressed_content


class ParquetStreamTransformer(StreamTransformer):
    def __init__(
        self,
        schema: pa.Schema,
        compression: str | None = None,
        compression_level: int | None = None,
        include_inserted_at: bool = False,
    ):
        super().__init__(compression=compression, include_inserted_at=include_inserted_at)
        self.schema = schema
        self.compression_level = compression_level

        # For Parquet, we need to handle schema and batching
        self._parquet_writer: pq.ParquetWriter | None = None
        self._parquet_buffer = BytesIO()

    @property
    def parquet_writer(self) -> pq.ParquetWriter:
        if self._parquet_writer is None:
            self._parquet_writer = pq.ParquetWriter(
                self._parquet_buffer,
                schema=self.schema,
                compression="none" if self.compression is None else self.compression,  # type: ignore
                compression_level=self.compression_level,
            )
        assert self._parquet_writer is not None
        return self._parquet_writer

    def finalize(self) -> typing.Generator[bytes, None, None]:
        """Ensure underlying Parquet writer is closed before flushing and closing temporary file."""
        yield from super().finalize()

        if self._parquet_writer is not None:
            self._parquet_writer.close()
            self._parquet_writer = None

            # Get final data without copying
            final_data = self._parquet_buffer.getvalue()
            if final_data:
                yield final_data

            # Cleanup
            self._parquet_buffer.close()
            self._parquet_buffer = BytesIO()

    def write_batch(self, record_batch: pa.RecordBatch) -> typing.Generator[bytes, None, None]:
        """Write records to a temporary file as Parquet."""

        self.parquet_writer.write_batch(record_batch.select(self.parquet_writer.schema.names))
        data = self._parquet_buffer.getvalue()

        self._parquet_buffer.seek(0)
        self._parquet_buffer.truncate(0)

        yield data


# TODO: Implement other transformers
