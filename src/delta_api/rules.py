"""Named, versioned rules that clients apply to stored values.

Each rule has an identifier such as "period-agreement/1". A changed rule gets a
new version. GET /api/rules describes every rule, so that an agent and the
frontend apply the same rule. These rules are display and selection rules.
They are not validated scientific criteria.
"""
from dataclasses import dataclass
import math


@dataclass(frozen=True)
class PeriodAgreement:
    """Display status of a target from the class label, the depth S/N, and the relative period error."""
    id: str = 'period-agreement/1'
    match_below: float = 0.01
    near_below: float = 0.05
    signal_snr_above: float = 100.0

    def status(self, label_class: str | None, depth_snr: float | None, relative_period_error: float | None,
               labels_visible: bool = True) -> str | None:
        """Return match, near, miss, signal, eb, or quiet. Return None when labels are withheld.

        A later blind policy withholds labels. This rule needs the class label for every status,
        so it gives no status without labels.
        """
        if not labels_visible:
            return None
        if label_class == 'eclipsing_binary':
            return 'eb'
        if label_class == 'no_known_planet':
            return 'signal' if depth_snr is not None and depth_snr > self.signal_snr_above else 'quiet'
        if relative_period_error is None:
            return 'quiet'
        if relative_period_error < self.match_below:
            return 'match'
        return 'near' if relative_period_error < self.near_below else 'miss'

    def describe(self) -> dict:
        return {
            'id': self.id, 'kind': 'display',
            'summary': 'Display status of a target for marker colours. Not a validation criterion.',
            'inputs': ['labels.class', 'search.depth_snr', 'reference_comparison.relative_period_error'],
            'requires_labels': True,
            'statuses': {
                'eb': 'label class eclipsing_binary',
                'signal': f'label class no_known_planet and depth S/N > {self.signal_snr_above:g}',
                'quiet': 'label class no_known_planet and depth S/N not above the limit, '
                         'or no relative period error',
                'match': f'relative period error < {self.match_below:g}',
                'near': f'relative period error >= {self.match_below:g} and < {self.near_below:g}',
                'miss': f'relative period error >= {self.near_below:g}',
            },
            'thresholds': {'match_below': self.match_below, 'near_below': self.near_below,
                           'signal_snr_above': self.signal_snr_above},
        }


@dataclass(frozen=True)
class DefaultRun:
    """Default run when a client names none: the analysis run with the most complete jobs. The newest wins a tie."""
    id: str = 'default-run/1'

    def choose(self, runs: list[dict]) -> dict | None:
        """runs: dicts with 'id', 'created_at', and 'complete'. Only runs with a complete job qualify."""
        best = None
        for run in runs:
            if run['complete'] <= 0:
                continue
            if best is None or (run['complete'], run['created_at'], str(run['id'])) > \
                    (best['complete'], best['created_at'], str(best['id'])):
                best = run
        return best

    def describe(self) -> dict:
        return {'id': self.id, 'kind': 'selection',
                'summary': 'The analysis run with the most complete jobs. The newest run wins a tie. '
                           'Filters: dataset and method.'}


@dataclass(frozen=True)
class FoldWindow:
    """Window and bin width of a fold view, from the stored BLS duration and the fold period."""
    id: str = 'fold-window/1'

    @staticmethod
    def window_hours(duration_days: float, period_days: float) -> float:
        return min(max(duration_days * 24 * 3, 3.0), 0.5 * period_days * 24)

    @staticmethod
    def bin_hours(window_hours: float) -> float:
        return window_hours / 24

    def describe(self) -> dict:
        return {'id': self.id, 'kind': 'display',
                'summary': 'Window: three BLS durations, at least 3 h, at most half the period. '
                           'Bins: 48 bins across the window. A bin needs at least 4 points. '
                           'Each bin gives the mean and the standard error of the mean.',
                'minimum_bin_count': 4}


@dataclass(frozen=True)
class BlsGrid:
    """Estimated size of the BLS period grid of one light curve (astropy BoxLeastSquares.autoperiod)."""
    id: str = 'bls-grid/1'

    @staticmethod
    def trials(baseline_days: float, minimum_period: float, maximum_period: float,
               durations: list[float], frequency_factor: float) -> int | None:
        if not (baseline_days > 0 and minimum_period > 0 and maximum_period > minimum_period
                and durations and frequency_factor > 0):
            return None
        spacing = frequency_factor * min(durations) / baseline_days ** 2
        return math.ceil((1 / minimum_period - 1 / maximum_period) / spacing)

    def describe(self) -> dict:
        return {'id': self.id, 'kind': 'estimate',
                'summary': 'Uniform frequency grid from 1/maximum_period to 1/minimum_period with spacing '
                           'frequency_factor × min(duration_days) / baseline². The baseline is time_stop − time_start. '
                           'The worker records the actual grid size as diagnostics.grid_trials.'}


@dataclass(frozen=True)
class CandidateStatus:
    """Current status of a candidate. The database view candidate_status applies this rule."""
    id: str = 'candidate-status/1'

    def describe(self) -> dict:
        return {'id': self.id, 'kind': 'status',
                'summary': 'The newest assessment on each axis gives the current status of a candidate. '
                           'Without an assessment, the evidence status is unreviewed and the identity status is unchecked. '
                           'A status is null when its source assessment is hidden from the client. '
                           'A decision does not change a status. An assessment with basis model_judgment cites a decision.',
                'axes': {'evidence': ['unreviewed', 'supported', 'ambiguous', 'rejected'],
                         'identity': ['unchecked', 'matched', 'unmatched', 'ambiguous']},
                'requires_labels': {'evidence': False, 'identity': True}}


PERIOD_AGREEMENT = PeriodAgreement()
CANDIDATE_STATUS = CandidateStatus()
DEFAULT_RUN = DefaultRun()
FOLD_WINDOW = FoldWindow()
BLS_GRID = BlsGrid()
