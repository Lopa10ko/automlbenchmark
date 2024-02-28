"""
**results** module provides the logic to format, save and read predictions generated by the *automl frameworks* (cf. ``TaskResult``),
as well as logic to compute, format, save, read and merge scores obtained from those predictions (cf. ``Result`` and ``Scoreboard``).
"""
from __future__ import annotations

from functools import partial
import collections
import io
import logging
import math
import os
import re
import statistics
from typing import Union

import numpy as np
from numpy import nan, sort
import pandas as pd
import scipy as sci
import scipy.sparse

from .data import Dataset, DatasetType, Feature
from .datautils import accuracy_score, auc, average_precision_score, balanced_accuracy_score, confusion_matrix, fbeta_score, log_loss, \
    mean_absolute_error, mean_squared_error, mean_squared_log_error, precision_recall_curve, r2_score, roc_auc_score, \
    read_csv, write_csv, is_data_frame, to_data_frame
from .resources import get as rget, config as rconfig, output_dirs
from .utils import Namespace, backup_file, cached, datetime_iso, get_metadata, json_load, memoize, profile, set_metadata

log = logging.getLogger(__name__)


A = Union[np.ndarray, sci.sparse.csr_matrix]
DF = pd.DataFrame
S = pd.Series

_supported_metrics_ = {}


def metric(higher_is_better=True):
    def decorator(fn):
        set_metadata(fn, higher_is_better=higher_is_better)
        _supported_metrics_[fn.__name__] = fn
        return fn
    return decorator


class NoResultError(Exception):
    pass


class ResultError(Exception):
    pass


class Scoreboard:

    results_file = 'results.csv'

    @classmethod
    def all(cls, scores_dir=None, autoload=True):
        return cls(scores_dir=scores_dir, autoload=autoload)

    @classmethod
    def from_file(cls, path):
        sep = rconfig().token_separator
        folder, basename = os.path.split(path)
        framework_name = None
        benchmark_name = None
        task_name = None
        patterns = [
            cls.results_file,
            rf"(?P<framework>[\w\-]+){sep}benchmark{sep}(?P<benchmark>[\w\-]+)\.csv",
            rf"benchmark{sep}(?P<benchmark>[\w\-]+)\.csv",
            rf"(?P<framework>[\w\-]+){sep}task{sep}(?P<task>[\w\-]+)\.csv",
            rf"task{sep}(?P<task>[\w\-]+)\.csv",
            r"(?P<framework>[\w\-]+)\.csv",
        ]
        found = False
        for pat in patterns:
            m = re.fullmatch(pat, basename)
            if m:
                found = True
                d = m.groupdict()
                benchmark_name = 'benchmark' in d and d['benchmark']
                task_name = 'task' in d and d['task']
                framework_name = 'framework' in d and d['framework']
                break

        if not found:
            return None

        scores_dir = None if path == basename else folder
        return cls(framework_name=framework_name, benchmark_name=benchmark_name, task_name=task_name, scores_dir=scores_dir)

    @staticmethod
    # @profile(logger=log)
    def load_df(file):
        name = file if isinstance(file, str) else type(file)
        log.debug("Loading scores from `%s`.", name)
        exists = isinstance(file, io.IOBase) or os.path.isfile(file)
        df = read_csv(file) if exists else to_data_frame({})
        log.debug("Loaded scores from `%s`.", name)
        return df

    @staticmethod
    # @profile(logger=log)
    def save_df(data_frame, path, append=False):
        exists = os.path.isfile(path)
        new_format = False
        df = data_frame
        if exists:
            head = read_csv(path, nrows=1)
            new_format = list(head.columns) != list(data_frame.columns)
        if new_format or (exists and not append):
            backup_file(path)
        if new_format and append:
            df = read_csv(path).append(data_frame)
        new_file = not exists or not append or new_format
        is_default_index = data_frame.index.name is None and not any(data_frame.index.names)
        log.debug("Saving scores to `%s`.", path)
        write_csv(df,
                  path=path,
                  header=new_file,
                  index=not is_default_index,
                  append=not new_file)
        log.info("Scores saved to `%s`.", path)

    def __init__(self, scores=None, framework_name=None, benchmark_name=None, task_name=None,
                 scores_dir=None, autoload=True):
        self.framework_name = framework_name
        self.benchmark_name = benchmark_name
        self.task_name = task_name
        self.scores_dir = (scores_dir if scores_dir
                           else output_dirs(rconfig().output_dir, rconfig().sid, ['scores']).scores)
        self.scores = (scores if scores is not None
                       else self.load_df(self.path) if autoload
                       else None)

    @cached
    def as_data_frame(self):
        # index = ['task', 'framework', 'fold']
        index = []
        df = (self.scores if is_data_frame(self.scores)
              else to_data_frame([dict(sc) for sc in self.scores]))
        if df.empty:
            # avoid dtype conversions during reindexing on empty frame
            return df
        fixed_cols = ['id', 'task', 'framework', 'constraint', 'fold', 'type', 'result', 'metric', 'mode', 'version',
                      'params', 'app_version', 'utc', 'duration', 'training_duration', 'predict_duration', 'models_count', 'seed', 'info']
        fixed_cols = [col for col in fixed_cols if col not in index]
        metrics_cols = [col for col in df.columns
                        if (col in dir(ClassificationResult) or col in dir(RegressionResult))
                        and not col.startswith('_')]
        metrics_cols.sort()
        dynamic_cols = [col for col in df.columns
                        if col not in index
                        and col not in fixed_cols
                        and col not in metrics_cols]
        dynamic_cols.sort()
        df = df.reindex(columns=[]+fixed_cols+metrics_cols+dynamic_cols)
        log.debug("Scores columns: %s.", df.columns)
        return df

    @memoize
    def as_printable_data_frame(self, verbosity=3):
        str_print = lambda val: '' if val in [None, '', 'None'] or (isinstance(val, float) and np.isnan(val)) else str(val)
        int_print = lambda val: int(val) if isinstance(val, (float, int)) and not np.isnan(val) else str_print(val)

        df = self.as_data_frame()
        if df.empty:
            return df

        force_str_cols = ['id']
        nanable_int_cols = ['fold', 'models_count', 'seed']
        low_precision_float_cols = ['duration', 'training_duration', 'predict_duration']
        high_precision_float_cols = [col for col in df.select_dtypes(include=[float]).columns if col not in ([] + nanable_int_cols + low_precision_float_cols)]
        for col in force_str_cols:
            df[col] = df[col].map(str_print)
        for col in nanable_int_cols:
            df[col] = df[col].map(int_print)
        for col in low_precision_float_cols:
            float_format = lambda f: ("{:.1g}" if f < 1 else "{:.1f}").format(f)
            # The .astype(float) is required to maintain NaN as 'NaN' instead of 'nan'
            df[col] = df[col].map(float_format).astype(float)
        for col in high_precision_float_cols:
            df[col] = df[col].map("{:.6g}".format).astype(float)

        cols = ([] if verbosity == 0
                else ['task', 'fold', 'framework', 'constraint', 'result', 'metric', 'info'] if verbosity == 1
                else ['id', 'task', 'fold', 'framework', 'constraint', 'result', 'metric',
                      'duration', 'seed', 'info'] if verbosity == 2
                else slice(None))
        return df.loc[:, cols]

    def load(self):
        self.scores = self.load_df(self.path)
        return self

    def save(self, append=False):
        self.save_df(self.as_printable_data_frame(), path=self.path, append=append)
        return self

    def append(self, board_or_df, no_duplicates=True):
        to_append = board_or_df.as_data_frame() if isinstance(board_or_df, Scoreboard) else board_or_df
        scores = self.as_data_frame().append(to_append)
        if no_duplicates:
            scores = scores.drop_duplicates()
        return Scoreboard(scores=scores,
                          framework_name=self.framework_name,
                          benchmark_name=self.benchmark_name,
                          task_name=self.task_name,
                          scores_dir=self.scores_dir)

    @property
    def path(self):
        sep = rconfig().token_separator
        if self.framework_name:
            if self.task_name:
                file_name = f"{self.framework_name}{sep}task_{self.task_name}.csv"
            elif self.benchmark_name:
                file_name = f"{self.framework_name}{sep}benchmark_{self.benchmark_name}.csv"
            else:
                file_name = f"{self.framework_name}.csv"
        else:
            if self.task_name:
                file_name = f"task_{self.task_name}.csv"
            elif self.benchmark_name:
                file_name = f"benchmark_{self.benchmark_name}.csv"
            else:
                file_name = Scoreboard.results_file

        return os.path.join(self.scores_dir, file_name)


class TaskResult:

    @staticmethod
    # @profile(logger=log)
    def load_predictions(predictions_file):
        log.info("Loading predictions from `%s`.", predictions_file)
        if os.path.isfile(predictions_file):
            try:
                df = read_csv(predictions_file, dtype=object)
                log.debug("Predictions preview:\n %s\n", df.head(10).to_string())

                if rconfig().test_mode:
                    TaskResult.validate_predictions(df)

                if 'repeated_item_id' in df.columns:
                    return TimeSeriesResult(df)
                else:
                    if df.shape[1] > 2:
                        return ClassificationResult(df)
                    else:
                        return RegressionResult(df)
            except Exception as e:
                return ErrorResult(ResultError(e))
        else:
            log.warning("Predictions file `%s` is missing: framework either failed or could not produce any prediction.", predictions_file)
            return NoResult("Missing predictions.")

    @staticmethod
    def load_metadata(metadata_file):
        log.info("Loading metadata from `%s`.", metadata_file)
        if os.path.isfile(metadata_file):
            return json_load(metadata_file, as_namespace=True)
        else:
            log.warning("Metadata file `%s` is missing: framework either couldn't start or implementation doesn't save metadata.", metadata_file)
            return Namespace(lambda: None)

    @staticmethod
    # @profile(logger=log)
    def save_predictions(dataset: Dataset, output_file: str,
                         predictions: Union[A, DF, S] = None, truth: Union[A, DF, S] = None,
                         probabilities: Union[A, DF] = None, probabilities_labels: Union[list, A] = None,
                         optional_columns: Union[A, DF] = None,
                         target_is_encoded: bool = False,
                         preview: bool = True):
        """ Save class probabilities and predicted labels to file in csv format.

        :param dataset:
        :param output_file:
        :param probabilities:
        :param predictions:
        :param truth:
        :param probabilities_labels:
        :param optional_columns:
        :param target_is_encoded:
        :param preview:
        :return: None
        """
        log.debug("Saving predictions to `%s`.", output_file)
        remap = None
        if isinstance(predictions, DF):
            predictions = predictions.squeeze()
        if isinstance(predictions, S):
            predictions = predictions.values
        if scipy.sparse.issparse(truth) and truth.shape[1] == 1:
            truth = pd.DataFrame(truth.todense())
        if isinstance(truth, DF):
            truth = truth.squeeze()
        if isinstance(truth, S):
            truth = truth.values
        if isinstance(probabilities, DF):
            probabilities = probabilities.values
        if probabilities_labels is not None:
            probabilities_labels = [str(label) for label in probabilities_labels]

        if probabilities is not None:
            prob_cols = probabilities_labels if probabilities_labels else dataset.target.label_encoder.classes
            df = to_data_frame(probabilities, columns=prob_cols)
            if probabilities_labels is not None:
                df = df[sort(prob_cols)]  # reorder columns alphabetically: necessary to match label encoding
                if any(prob_cols != df.columns.values):
                    encoding_map = {prob_cols.index(col): i for i, col in enumerate(df.columns.values)}
                    remap = np.vectorize(lambda v: encoding_map[v])
        else:
            df = to_data_frame(None)

        preds = predictions
        truth = truth if truth is not None else dataset.test.y
        if not _encode_predictions_and_truth_ and target_is_encoded:
            if remap:
                predictions = remap(predictions)
                truth = remap(truth)
            preds = dataset.target.label_encoder.inverse_transform(predictions)
            truth = dataset.target.label_encoder.inverse_transform(truth)
        if _encode_predictions_and_truth_ and not target_is_encoded:
            preds = dataset.target.label_encoder.transform(predictions)
            truth = dataset.target.label_encoder.transform(truth)

        df = df.assign(predictions=preds)
        df = df.assign(truth=truth)

        if optional_columns is not None:
            df = pd.concat([df, optional_columns], axis=1)  # type: ignore # int not seen as valid Axis

        if preview:
            log.info("Predictions preview:\n %s\n", df.head(20).to_string())
        backup_file(output_file)
        write_csv(df, path=output_file)
        log.info("Predictions saved to `%s`.", output_file)

    @staticmethod
    def validate_predictions(predictions: pd.DataFrame):
        names = predictions.columns.values
        assert len(names) >= 2, "predictions frame should have 2 columns (regression) or more (classification)"
        assert names[-1] == "truth", "last column of predictions frame must be named `truth`"
        assert names[-2] == "predictions", "last column of predictions frame must be named `predictions`"
        if len(names) == 2:  # regression
            for name, col in predictions.items():
                pd.to_numeric(col)  # pandas will raise if we have non-numerical values
        else:  # classification
            predictors = names[:-2]
            probabilities, preds, truth = predictions.iloc[:,:-2], predictions.iloc[:,-2], predictions.iloc[:,-1]
            assert np.array_equal(predictors, np.sort(predictors)), "Predictors columns are not sorted in lexicographic order."
            assert set(np.unique(predictors)) == set(predictors), "Predictions contain multiple columns with the same label."
            for name, col in probabilities.items():
                pd.to_numeric(col)  # pandas will raise if we have non-numerical values

            if _encode_predictions_and_truth_:
                assert np.array_equal(truth, truth.astype(int)), "Values in truth column are not encoded."
                assert np.array_equal(preds, preds.astype(int)), "Values in predictions column are not encoded."
                predictors_set = set(range(len(predictors)))
                validate_row = lambda r: r[:-2].astype(float).values.argmax() == r[-2]
            else:
                predictors_set = set(predictors)
                validate_row = lambda r: r[:-2].astype(float).idxmax() == r[-2]

            truth_set = set(truth.unique())
            if predictors_set < truth_set:
                log.warning("Truth column contains values unseen during training: no matching probability column.")
            if predictors_set > truth_set:
                log.warning("Truth column doesn't contain all the possible target values: the test dataset may be too small.")
            predictions_set = set(preds.unique())
            assert predictions_set <= predictors_set, "Predictions column contains unexpected values: {}.".format(predictions_set - predictors_set)
            assert predictions.apply(validate_row, axis=1).all(), "Predictions don't always match the predictor with the highest probability."

    @classmethod
    def score_from_predictions_file(cls, path):
        sep = rconfig().token_separator
        folder, basename = os.path.split(path)
        folder_g = collections.defaultdict(lambda: None)
        if folder:
            folder_pat = rf"/(?P<framework>[\w\-]+?){sep}(?P<benchmark>[\w\-]+){sep}(?P<constraint>[\w\-]+){sep}(?P<mode>[\w\-]+)({sep}(?P<datetime>\d{8}T\d{6}))/"
            folder_m = re.match(folder_pat, folder)
            if folder_m:
                folder_g = folder_m.groupdict()

        file_pat = rf"(?P<framework>[\w\-]+?){sep}(?P<task>[\w\-]+){sep}(?P<fold>\d+)\.csv"
        file_m = re.fullmatch(file_pat, basename)
        if not file_m:
            log.error("Predictions file `%s` has wrong naming format.", path)
            return None

        file_g = file_m.groupdict()
        task_name = file_g['task']
        fold = int(file_g['fold'])
        constraint = folder_g['constraint']
        benchmark = folder_g['benchmark']
        task = Namespace(name=task_name, id=task_name)
        if benchmark:
            try:
                tasks, _, _ = rget().benchmark_definition(benchmark)
                task = next(t for t in tasks if t.name == task_name)
            except:
                pass

        task_result = cls(task, fold, constraint, predictions_dir=path)
        return task_result.compute_score()

    def __init__(self, task_def, fold: int, constraint: str, predictions_dir: str | None = None, metadata: Namespace = None):
        self.task = task_def
        self.fold = fold
        self.constraint = constraint
        self.predictions_dir = (predictions_dir if predictions_dir
                                else output_dirs(rconfig().output_dir, rconfig().sid, ['predictions']).predictions)
        self._metadata = metadata

    @cached
    def get_result(self):
        return self.load_predictions(self._predictions_file)

    @cached
    def get_result_metadata(self):
        return self._metadata or self.load_metadata(self._metadata_file)

    @profile(logger=log)
    def compute_score(self, result=None, meta_result=None):
        meta_result = Namespace({} if meta_result is None else meta_result)
        metadata = self.get_result_metadata()
        entry = Namespace(
            id=self.task.id,
            task=self.task.name,
            type=metadata.type_,
            constraint=self.constraint,
            framework=metadata.framework,
            version=metadata.framework_version,
            params=repr(metadata.framework_params) if metadata.framework_params else '',
            fold=self.fold,
            mode=rconfig().run_mode,
            seed=metadata.seed,
            app_version=rget().app_version,
            utc=datetime_iso(),
            metric=metadata.metric,
            duration=nan
        )
        required_meta_res = ['training_duration', 'predict_duration', 'models_count']
        for m in required_meta_res:
            entry[m] = meta_result[m] if m in meta_result else nan

        if inference_times := Namespace.get(meta_result, "inference_times"):
            for data_type, measurements in Namespace.dict(inference_times).items():
                for n_samples, measured_times in Namespace.dict(measurements).items():
                    entry[f"infer_batch_size_{data_type}_{n_samples}"] = np.median(measured_times)
        result = self.get_result() if result is None else result

        scoring_errors = []

        def do_score(m):
            score = result.evaluate(m)
            if 'message' in score:
                scoring_errors.append(score.message)
            return score

        def set_score(score):
            entry.metric = score.metric
            entry.result = score.value
            if score.higher_is_better is False:  # if unknown metric, and higher_is_better is None, then no change
                entry.metric = f"neg_{entry.metric}"
                entry.result = - entry.result

        for metric in metadata.metrics or []:
            sc = do_score(metric)
            entry[metric] = sc.value
            if metric == entry.metric:
                set_score(sc)

        if 'result' not in entry:
            set_score(do_score(entry.metric))

        entry.info = result.info
        if scoring_errors:
            entry.info = "; ".join(filter(lambda it: it, [entry.info, *scoring_errors]))
        entry |= Namespace({k: v for k, v in meta_result if k not in required_meta_res and k != "inference_times"})
        log.info("Metric scores: %s", entry)
        return entry

    @property
    def _predictions_file(self):
        return os.path.join(self.predictions_dir, self.task.name, str(self.fold), "predictions.csv")

    @property
    def _metadata_file(self):
        return os.path.join(self.predictions_dir, self.task.name, str(self.fold), "metadata.json")


class Result:

    def __init__(self, predictions_df, info=None):
        self.df = predictions_df
        self.info = info
        self.truth = self.df.iloc[:, -1].values if self.df is not None else None
        self.predictions = self.df.iloc[:, -2].values if self.df is not None else None
        self.target = None
        self.type = None

    def evaluate(self, metric):
        eval_res = Namespace(metric=metric)
        if hasattr(self, metric):
            metric_fn = getattr(self, metric)
            eval_res.higher_is_better = get_metadata(metric_fn, 'higher_is_better')
            try:
                eval_res.value = metric_fn()
            except Exception as e:
                log.exception("Failed to compute metric %s: ", metric, e)
                eval_res += Namespace(value=nan, message=f"Scoring {metric}: {str(e)}")
        else:
            pb_type = self.type.name if self.type is not None else 'unknown'
            # raise ValueError(f"Metric {metric} is not supported for {pb_type}.")
            log.warning("Metric %s is not supported for %s problems!", metric, pb_type)
            eval_res += Namespace(value=nan, higher_is_better=None, message=f"Unsupported metric `{metric}` for {pb_type} problems")
        return eval_res


class NoResult(Result):

    def __init__(self, info=None):
        super().__init__(None, info)
        self.missing_result = np.nan

    def evaluate(self, metric):
        eval_res = Namespace(metric=metric, value=self.missing_result)
        if metric is None:
            eval_res += Namespace(higher_is_better=None)
        elif metric not in _supported_metrics_:
            eval_res += Namespace(higher_is_better=None, message=f"Unsupported metric `{metric}`")
        else:
            eval_res.higher_is_better = get_metadata(_supported_metrics_.get(metric), 'higher_is_better')
        return eval_res


class ErrorResult(NoResult):

    def __init__(self, error):
        msg = "{}: {}".format(type(error).__qualname__ if error is not None else "Error", error)
        max_len = rconfig().results.error_max_length
        msg = msg if len(msg) <= max_len else (msg[:max_len - 1] + '…')
        super().__init__(msg)


class ClassificationResult(Result):

    multi_class_average = 'weighted'  # used by metrics like fbeta or auc

    def __init__(self, predictions_df, info=None):
        super().__init__(predictions_df, info)
        self.classes = self.df.columns[:-2].values.astype(str, copy=False)
        self.probabilities = self.df.iloc[:, :-2].values.astype(float, copy=False)
        self.target = Feature(0, 'target', 'category', values=self.classes, is_target=True)
        self.type = DatasetType.binary if len(self.classes) == 2 else DatasetType.multiclass
        self.truth = self._autoencode(self.truth.astype(str, copy=False))
        self.predictions = self._autoencode(self.predictions.astype(str, copy=False))
        self.labels = self._autoencode(self.classes)

    @metric(higher_is_better=True)
    def acc(self):
        """Accuracy"""
        return float(accuracy_score(self.truth, self.predictions))

    @metric(higher_is_better=True)
    def auc(self):
        """Area Under (ROC) Curve, computed on probabilities, not on predictions"""
        if self.type == DatasetType.multiclass:
            raise ResultError("For multiclass problems, use `auc_ovr` or `auc_ovo` metrics instead of `auc`.")
        else:
            return float(roc_auc_score(self.truth, self.probabilities[:, 1]))

    @metric(higher_is_better=True)
    def auc_ovo(self):
        """AUC One-vs-One"""
        return self._auc_multi(mc='ovo')

    @metric(higher_is_better=True)
    def auc_ovr(self):
        """AUC One-vs-Rest"""
        return self._auc_multi(mc='ovr')

    @metric(higher_is_better=True)
    def balacc(self):
        """Balanced accuracy"""
        return float(balanced_accuracy_score(self.truth, self.predictions))

    @metric(higher_is_better=True)
    def f05(self):
        """F-beta 0.5"""
        return self._fbeta(0.5)

    @metric(higher_is_better=True)
    def f1(self):
        """F-beta 1"""
        return self._fbeta(1)

    @metric(higher_is_better=True)
    def f2(self):
        """F-beta 2"""
        return self._fbeta(2)

    @metric(higher_is_better=False)
    def logloss(self):
        """Log Loss"""
        return float(log_loss(self.truth, self.probabilities, labels=self.labels))

    @metric(higher_is_better=False)
    def max_pce(self):
        """Max per Class Error"""
        return max(self._per_class_errors())

    @metric(higher_is_better=False)
    def mean_pce(self):
        """Mean per Class Error"""
        return statistics.mean(self._per_class_errors())

    @metric(higher_is_better=True)
    def pr_auc(self):
        """Precision Recall AUC"""
        if self.type != DatasetType.binary:
            raise ResultError("PR AUC metric is only available for binary problems.")
        else:
            # precision, recall, thresholds = precision_recall_curve(self.truth, self.probabilities[:, 1])
            # return float(auc(recall, precision))
            return float(average_precision_score(self.truth, self.probabilities[:, 1]))

    def _autoencode(self, vec):
        needs_encoding = not _encode_predictions_and_truth_ or (isinstance(vec[0], str) and not vec[0].isdigit())
        return self.target.label_encoder.transform(vec) if needs_encoding else vec

    def _auc_multi(self, mc='raise'):
        average = ClassificationResult.multi_class_average
        return float(roc_auc_score(self.truth, self.probabilities, average=average, labels=self.labels, multi_class=mc))

    def _cm(self):
        return confusion_matrix(self.truth, self.predictions, labels=self.labels)

    def _fbeta(self, beta):
        average = ClassificationResult.multi_class_average if self.type == DatasetType.multiclass else 'binary'
        return float(fbeta_score(self.truth, self.predictions, beta=beta, average=average, labels=self.labels))

    def _per_class_errors(self):
        return [(s-d)/s for s, d in ((sum(r), r[i]) for i, r in enumerate(self._cm()))]


class RegressionResult(Result):

    def __init__(self, predictions_df, info=None):
        super().__init__(predictions_df, info)
        self.truth = self.truth.astype(float, copy=False)
        self.target = Feature(0, 'target', 'real', is_target=True)
        self.type = DatasetType.regression

    @metric(higher_is_better=False)
    def mae(self):
        """Mean Absolute Error"""
        return float(mean_absolute_error(self.truth, self.predictions))

    @metric(higher_is_better=False)
    def mse(self):
        """Mean Squared Error"""
        return float(mean_squared_error(self.truth, self.predictions))

    @metric(higher_is_better=False)
    def msle(self):
        """Mean Squared Logarithmic Error"""
        return float(mean_squared_log_error(self.truth, self.predictions))

    @metric(higher_is_better=False)
    def rmse(self):
        """Root Mean Square Error"""
        return math.sqrt(self.mse())

    @metric(higher_is_better=False)
    def rmsle(self):
        """Root Mean Square Logarithmic Error"""
        return math.sqrt(self.msle())

    @metric(higher_is_better=True)
    def r2(self):
        """R^2"""
        return float(r2_score(self.truth, self.predictions))


class TimeSeriesResult(RegressionResult):
    def __init__(self, predictions_df, info=None):
        super().__init__(predictions_df, info)
        required_columns = {'truth', 'predictions', 'repeated_item_id', 'repeated_abs_seasonal_error'}
        if required_columns - set(self.df.columns):
            raise ValueError(f'Missing columns for calculating time series metrics: {required_columns - set(self.df.columns)}.')

        quantile_columns = [column for column in self.df.columns if column.startswith('0.')]
        unrecognized_columns = [column for column in self.df.columns if column not in required_columns and column not in quantile_columns]
        if len(unrecognized_columns) > 0:
            raise ValueError(f'Predictions contain unrecognized columns: {unrecognized_columns}.')

        self.type = DatasetType.timeseries
        self.truth = self.df['truth'].values.astype(float)
        self.item_ids = self.df['repeated_item_id'].values
        self.abs_seasonal_error = self.df['repeated_abs_seasonal_error'].values.astype(float)
        # predictions = point forecast, quantile_predictions = quantile forecast
        self.predictions = self.df['predictions'].values.astype(float)
        self.quantile_predictions = self.df[quantile_columns].values.astype(float)
        self.quantile_levels = np.array(quantile_columns, dtype=float)

        if (~np.isfinite(self.predictions)).any() or (~np.isfinite(self.quantile_predictions)).any():
            raise ValueError('Predictions contain NaN or Inf values')

        _, unique_item_ids_counts = np.unique(self.item_ids, return_counts=True)
        if len(set(unique_item_ids_counts)) != 1:
            raise ValueError(f'Error: Predicted sequences have different lengths {unique_item_ids_counts}.')

    def _itemwise_mean(self, values):
        """Compute mean for each time series."""
        return pd.Series(values).groupby(self.item_ids, sort=False).mean().values

    def _safemean(self, values):
        """Compute mean, while ignoring nan, +inf, -inf values."""
        return np.mean(values[np.isfinite(values)])

    @metric(higher_is_better=False)
    def smape(self):
        """Symmetric Mean Absolute Percentage Error"""
        num = np.abs(self.truth - self.predictions)
        denom = (np.abs(self.truth) + np.abs(self.predictions)) / 2
        return self._safemean(num / denom)

    @metric(higher_is_better=False)
    def mape(self):
        """Mean Absolute Percentage Error"""
        num = np.abs(self.truth - self.predictions)
        denom = np.abs(self.truth)
        return self._safemean(num / denom)

    @metric(higher_is_better=False)
    def wape(self):
        """Weighted Average Percentage Error"""
        return np.sum(np.abs(self.truth - self.predictions)) / np.sum(np.abs(self.truth))

    @metric(higher_is_better=False)
    def mase(self):
        """Mean Absolute Scaled Error

        Error for each item is normalized by the in-sample error of the naive forecaster.
        This makes scores comparable across different items.
        """
        error = np.abs(self.truth - self.predictions)
        error_per_item = self._itemwise_mean(error / self.abs_seasonal_error)
        return self._safemean(error_per_item)

    def _quantile_loss_per_step(self):
        # Array of shape [len(self.predictions), len(self.quantile_levels)]
        return 2 * np.abs(
            (self.quantile_predictions - self.truth[:, None])
            * ((self.quantile_predictions >= self.truth[:, None]) - self.quantile_levels)
        )

    @metric(higher_is_better=False)
    def mql(self):
        """Quantile Loss, also known as Pinball Loss, averaged across all quantile levels & time steps.

        Equivalent to the Weighted Interval Score if the quantile_levels are symmetric around 0.5

        Approximates the Continuous Ranked Probability Score
        """
        return np.mean(self._quantile_loss_per_step())

    @metric(higher_is_better=False)
    def wql(self):
        """Weighted Quantile Loss.

        Defined as total quantile loss normalized by the total abs value of target time series.
        """
        return self._quantile_loss_per_step().mean(axis=1).sum() / np.sum(np.abs(self.truth))

    @metric(higher_is_better=False)
    def sql(self):
        """Scaled Quantile Loss, also known as Scaled Pinball Loss.

        Similar to MASE, the quantile loss for each item is normalized by the in-sample error of the naive forecaster.
        This makes scores comparable across different items.
        """
        pl_per_item = self._itemwise_mean(self._quantile_loss_per_step().mean(axis=1) / self.abs_seasonal_error)
        return self._safemean(pl_per_item)


_encode_predictions_and_truth_ = False

save_predictions = TaskResult.save_predictions
