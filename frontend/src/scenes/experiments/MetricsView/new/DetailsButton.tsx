import { IconGraph } from '@posthog/icons'
import { LemonButton } from '@posthog/lemon-ui'
import { SharedMetric } from 'scenes/experiments/SharedMetrics/sharedMetricLogic'

import {
    ExperimentFunnelsQuery,
    ExperimentMetric,
    ExperimentTrendsQuery,
    NodeKind,
} from '~/queries/schema/schema-general'
import { Experiment } from '~/types'

export function DetailsButton({
    metric,
    isSecondary,
    experiment,
    setIsModalOpen,
}: {
    metric: ExperimentMetric | ExperimentTrendsQuery | ExperimentFunnelsQuery
    isSecondary: boolean
    experiment: Experiment
    setIsModalOpen: (isOpen: boolean) => void
}): JSX.Element {
    if (metric.kind !== NodeKind.ExperimentMetric || metric.metric_type !== 'funnel') {
        return <></>
    }

    const primaryMetricsLength =
        experiment.metrics.length +
        experiment.saved_metrics.filter((savedMetric: SharedMetric) => savedMetric.metadata?.type === 'primary').length
    return (
        <>
            {(isSecondary || (!isSecondary && primaryMetricsLength > 1)) && (
                <div
                    className="absolute bottom-2 left-2 flex justify-center bg-[var(--bg-table)] z-[101]"
                    // Chart is z-index 100, so we need to be above it
                >
                    <LemonButton
                        type="secondary"
                        size="xsmall"
                        icon={<IconGraph />}
                        onClick={() => setIsModalOpen(true)}
                    >
                        Details
                    </LemonButton>
                </div>
            )}
        </>
    )
}
