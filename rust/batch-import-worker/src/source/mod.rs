use anyhow::Error;
use async_trait::async_trait;

pub mod folder;
pub mod s3;
pub mod url_list;

#[async_trait]
pub trait DataSource: Sync + Send {
    async fn keys(&self) -> Result<Vec<String>, Error>;
    async fn size(&self, key: &str) -> Result<u64, Error>;
    async fn get_chunk(&self, key: &str, offset: u64, size: u64) -> Result<Vec<u8>, Error>;
}
