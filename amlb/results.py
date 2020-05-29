"""
**results** module provides the logic to format, save and read predictions generated by the *automl frameworks* (cf. ``TaskResult``),
as well as logic to compute, format, save, read and merge scores obtained from those predictions (cf. ``Result`` and ``Scoreboard``).
"""
from functools import partial
import io
import logging
import math
import os
import re
import statistics

import numpy as np
from numpy import nan, sort

from .data import Dataset, DatasetType, Feature
from .datautils import accuracy_score, confusion_matrix, f1_score, log_loss, balanced_accuracy_score, mean_absolute_error, mean_squared_error, mean_squared_log_error, r2_score, roc_auc_score, read_csv, write_csv, is_data_frame, to_data_frame
from .resources import get as rget, config as rconfig, output_dirs
from .utils import Namespace, backup_file, cached, datetime_iso, memoize, profile

log = logging.getLogger(__name__)


class NoResultError(Exception):
    pass


# TODO: reconsider organisation of output files:
#   predictions: add framework version to name, timestamp? group into subdirs?

class Scoreboard:

    results_file = 'results.csv'

    @classmethod
    def all(cls, scores_dir=None):
        return cls(scores_dir=scores_dir)

    @classmethod
    def from_file(cls, path):
        folder, basename = os.path.split(path)
        framework_name = None
        benchmark_name = None
        task_name = None
        patterns = [
            cls.results_file,
            r"(?P<framework>[\w\-]+)_benchmark_(?P<benchmark>[\w\-]+)\.csv",
            r"benchmark_(?P<benchmark>[\w\-]+)\.csv",
            r"(?P<framework>[\w\-]+)_task_(?P<task>[\w\-]+)\.csv",
            r"task_(?P<task>[\w\-]+)\.csv",
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
        if exists:
            df = read_csv(path, nrows=1)
            new_format = list(df.columns) != list(data_frame.columns)
        if new_format or (exists and not append):
            backup_file(path)
        new_file = not exists or not append or new_format
        is_default_index = data_frame.index.name is None and not any(data_frame.index.names)
        log.debug("Saving scores to `%s`.", path)
        write_csv(data_frame,
                  path=path,
                  header=new_file,
                  index=not is_default_index,
                  append=not new_file)
        log.info("Scores saved to `%s`.", path)

    def __init__(self, scores=None, framework_name=None, benchmark_name=None, task_name=None, scores_dir=None):
        self.framework_name = framework_name
        self.benchmark_name = benchmark_name
        self.task_name = task_name
        self.scores_dir = scores_dir if scores_dir \
            else output_dirs(rconfig().output_dir, rconfig().sid, ['scores']).scores
        self.scores = scores if scores is not None else self._load()

    @cached
    def as_data_frame(self):
        # index = ['task', 'framework', 'fold']
        index = []
        df = self.scores if is_data_frame(self.scores) \
            else to_data_frame([dict(sc) for sc in self.scores])
        if df.empty:
            # avoid dtype conversions during reindexing on empty frame
            return df
        fixed_cols = ['id', 'task', 'framework', 'constraint', 'fold', 'result', 'metric', 'mode', 'version',
                      'params', 'tag', 'utc', 'duration', 'models', 'seed', 'info']
        fixed_cols = [col for col in fixed_cols if col not in index]
        dynamic_cols = [col for col in df.columns if col not in index and col not in fixed_cols]
        dynamic_cols.sort()
        df = df.reindex(columns=[]+fixed_cols+dynamic_cols)
        log.debug("Scores columns: %s.", df.columns)
        return df

    @cached
    def as_printable_data_frame(self):
        str_print = lambda val: '' if val in [None, '', 'None'] or (isinstance(val, float) and np.isnan(val)) else val
        int_print = lambda val: int(val) if isinstance(val, float) and not np.isnan(val) else str_print(val)
        num_print = lambda fn, val: None if isinstance(val, str) else fn(val)

        df = self.as_data_frame()
        force_str_cols = ['id']
        nanable_int_cols = ['fold', 'models', 'seed']
        low_precision_float_cols = ['duration']
        high_precision_float_cols = [col for col in df.select_dtypes(include=[np.float]).columns if col not in ([] + nanable_int_cols + low_precision_float_cols)]
        for col in force_str_cols:
            df[col] = df[col].astype(np.object).map(str_print).astype(np.str)
        for col in nanable_int_cols:
            df[col] = df[col].astype(np.object).map(int_print).astype(np.str)
        for col in low_precision_float_cols:
            df[col] = df[col].astype(np.float).map(partial(num_print, "{:.1f}".format)).astype(np.float)
        for col in high_precision_float_cols:
            df[col] = df[col].map(partial(num_print, "{:.6g}".format)).astype(np.float)
        return df

    def _load(self):
        return self.load_df(self._score_file())

    def save(self, append=False):
        self.save_df(self.as_printable_data_frame(), path=self._score_file(), append=append)

    def append(self, board_or_df, no_duplicates=True):
        to_append = board_or_df.as_data_frame() if isinstance(board_or_df, Scoreboard) else board_or_df
        scores = self.as_data_frame().append(to_append, sort=False)
        if no_duplicates:
            scores = scores.drop_duplicates()
        return Scoreboard(scores=scores,
                          framework_name=self.framework_name,
                          benchmark_name=self.benchmark_name,
                          task_name=self.task_name,
                          scores_dir=self.scores_dir)

    def _score_file(self):
        if self.framework_name:
            if self.task_name:
                file_name = "{framework}_task_{task}.csv".format(framework=self.framework_name, task=self.task_name)
            elif self.benchmark_name:
                file_name = "{framework}_benchmark_{benchmark}.csv".format(framework=self.framework_name, benchmark=self.benchmark_name)
            else:
                file_name = "{framework}.csv".format(framework=self.framework_name)
        else:
            if self.task_name:
                file_name = "task_{task}.csv".format(task=self.task_name)
            elif self.benchmark_name:
                file_name = "benchmark_{benchmark}.csv".format(benchmark=self.benchmark_name)
            else:
                file_name = Scoreboard.results_file

        return os.path.join(self.scores_dir, file_name)


class TaskResult:

    @staticmethod
    # @profile(logger=log)
    def load_predictions(predictions_file):
        log.info("Loading predictions from `%s`.", predictions_file)
        if os.path.isfile(predictions_file):
            df = read_csv(predictions_file, dtype=object)
            log.debug("Predictions preview:\n %s\n", df.head(10).to_string())
            if df.shape[1] > 2:
                return ClassificationResult(df)
            else:
                return RegressionResult(df)
        else:
            log.warning("Predictions file `%s` is missing: framework either failed or could not produce any prediction.", predictions_file)
            return NoResult("Missing predictions.")

    @staticmethod
    # @profile(logger=log)
    def save_predictions(dataset: Dataset, output_file: str,
                         predictions=None, truth=None,
                         probabilities=None, probabilities_labels=None,
                         target_is_encoded=False):
        """ Save class probabilities and predicted labels to file in csv format.

        :param dataset:
        :param output_file:
        :param probabilities:
        :param predictions:
        :param truth:
        :param probabilities_labels:
        :param target_is_encoded:
        :return: None
        """
        log.debug("Saving predictions to `%s`.", output_file)
        if probabilities is not None:
            prob_cols = probabilities_labels if probabilities_labels else dataset.target.label_encoder.classes
            df = to_data_frame(probabilities, columns=prob_cols)
            if probabilities_labels:
                df = df[sort(prob_cols)]  # reorder columns alphabetically: necessary to match label encoding
        else:
            df = to_data_frame(None)

        preds = predictions
        truth = truth if truth is not None else dataset.test.y
        if not _encode_predictions_and_truth_ and target_is_encoded:
            preds = dataset.target.label_encoder.inverse_transform(predictions)
            truth = dataset.target.label_encoder.inverse_transform(truth)
        if _encode_predictions_and_truth_ and not target_is_encoded:
            preds = dataset.target.label_encoder.transform(predictions)
            truth = dataset.target.label_encoder.transform(truth)

        df = df.assign(predictions=preds)
        df = df.assign(truth=truth)
        log.info("Predictions preview:\n %s\n", df.head(20).to_string())
        backup_file(output_file)
        write_csv(df, path=output_file)
        log.info("Predictions saved to `%s`.", output_file)

    @classmethod
    def score_from_predictions_file(cls, path):
        folder, basename = os.path.split(path)
        pattern = r"(?P<framework>[\w\-]+?)_(?P<task>[\w\-]+)_(?P<fold>\d+)(_(?P<datetime>\d{8}T\d{6}))?.csv"
        m = re.fullmatch(pattern, basename)
        if not m:
            log.error("Predictions file `%s` has wrong naming format.", path)
            return None

        d = m.groupdict()
        framework_name = d['framework']
        task_name = d['task']
        fold = int(d['fold'])
        result = cls.load_predictions(path)
        task_result = cls(task_name, fold, '')
        metrics = rconfig().benchmarks.metrics.get(result.type.name)
        return task_result.compute_scores(framework_name, metrics, result=result)

    def __init__(self, task_def, fold: int, constraint: str, predictions_dir=None):
        self.task = task_def
        self.fold = fold
        self.constraint = constraint
        self.predictions_dir = (predictions_dir if predictions_dir
                                else output_dirs(rconfig().output_dir, rconfig().sid, ['predictions']).predictions)

    @memoize
    def get_result(self, framework_name):
        return self.load_predictions(self._predictions_file(framework_name))

    @profile(logger=log)
    def compute_scores(self, framework_name, metrics, result=None, meta_result=None):
        framework_def, _ = rget().framework_definition(framework_name)
        meta_result = Namespace({} if meta_result is None else meta_result)
        scores = Namespace(
            id=self.task.id,
            task=self.task.name,
            constraint=self.constraint,
            framework=framework_name,
            version=framework_def.version,
            params=(str(meta_result.params) if 'params' in meta_result and bool(meta_result.params)
                    else str(framework_def.params) if len(framework_def.params) > 0
                    else ''),
            fold=self.fold,
            mode=rconfig().run_mode,
            seed=rget().seed(self.fold),
            tag=rget().project_info.tag,
            utc=datetime_iso(),
            duration=meta_result.training_duration if 'training_duration' in meta_result else nan,
            models=meta_result.models_count if 'models_count' in meta_result else nan,
        )
        result = self.get_result(framework_name) if result is None else result
        for metric in metrics:
            score = result.evaluate(metric)
            scores[metric] = score
        scores.metric = metrics[0] if len(metrics) > 0 else ''
        scores.result = scores[scores.metric] if scores.metric in scores else result.evaluate(scores.metric)
        scores.info = result.info
        scores % Namespace({k: v for k, v in meta_result if k not in ['models_count', 'training_duration']})
        log.info("Metric scores: %s", scores)
        return scores

    def _predictions_file(self, framework_name):
        return os.path.join(self.predictions_dir, "{framework}_{task}_{fold}.csv").format(
            framework=framework_name.lower(),
            task=self.task.name,
            fold=self.fold
        )


class Result:

    def __init__(self, predictions_df, info=None):
        self.df = predictions_df
        self.info = info
        self.truth = self.df.iloc[:, -1].values if self.df is not None else None
        self.predictions = self.df.iloc[:, -2].values if self.df is not None else None
        self.target = None
        self.type = None

    def evaluate(self, metric):
        if hasattr(self, metric):
            return getattr(self, metric)()
        # raise ValueError("Metric {metric} is not supported for {type}.".format(metric=metric, type=self.type))
        log.warning("Metric %s is not supported for %s!", metric, self.type)
        return nan


class NoResult(Result):

    def __init__(self, info=None):
        super().__init__(None, info)
        self.missing_result = np.nan

    def evaluate(self, metric):
        return self.missing_result


class ErrorResult(NoResult):

    def __init__(self, error):
        msg = "{}: {}".format(type(error).__qualname__ if error is not None else "Error", error)
        max_len = rconfig().results.error_max_length
        msg = msg if len(msg) <= max_len else (msg[:max_len - 3] + '...')
        super().__init__(msg)


class ClassificationResult(Result):

    def __init__(self, predictions_df, info=None):
        super().__init__(predictions_df, info)
        self.classes = self.df.columns[:-2].values.astype(str, copy=False)
        self.probabilities = self.df.iloc[:, :-2].values.astype(float, copy=False)
        self.target = Feature(0, 'class', 'categorical', self.classes, is_target=True)
        self.type = DatasetType.binary if len(self.classes) == 2 else DatasetType.multiclass
        self.truth = self._autoencode(self.truth.astype(str, copy=False))
        self.predictions = self._autoencode(self.predictions.astype(str, copy=False))

    def acc(self):
        return float(accuracy_score(self.truth, self.predictions))

    def balacc(self):
        return float(balanced_accuracy_score(self.truth, self.predictions))

    def auc(self):
        if self.type != DatasetType.binary:
            # raise ValueError("AUC metric is only supported for binary classification: {}.".format(self.classes))
            log.warning("AUC metric is only supported for binary classification: %s.", self.classes)
            return nan
        return float(roc_auc_score(self.truth, self.probabilities[:, 1]))

    def cm(self):
        return confusion_matrix(self.truth, self.predictions)

    def _per_class_errors(self):
        return [(s-d)/s for s, d in ((sum(r), r[i]) for i, r in enumerate(self.cm()))]

    def mean_pce(self):
        """mean per class error"""
        return statistics.mean(self._per_class_errors())

    def max_pce(self):
        """max per class error"""
        return max(self._per_class_errors())

    def f1(self):
        return float(f1_score(self.truth, self.predictions))

    def logloss(self):
        # truth_enc = self.target.one_hot_encoder.transform(self.truth)
        return float(log_loss(self.truth, self.probabilities))

    def _autoencode(self, vec):
        needs_encoding = not _encode_predictions_and_truth_ or (isinstance(vec[0], str) and not vec[0].isdigit())
        return self.target.label_encoder.transform(vec) if needs_encoding else vec


class RegressionResult(Result):

    def __init__(self, predictions_df, info=None):
        super().__init__(predictions_df, info)
        self.truth = self.truth.astype(float, copy=False)
        self.target = Feature(0, 'target', 'real', is_target=True)
        self.type = DatasetType.regression

    def mae(self):
        return float(mean_absolute_error(self.truth, self.predictions))

    def mse(self):
        return float(mean_squared_error(self.truth, self.predictions))

    def msle(self):
        return float(mean_squared_log_error(self.truth, self.predictions))

    def rmse(self):
        return math.sqrt(self.mse())

    def rmsle(self):
        return math.sqrt(self.msle())

    def r2(self):
        return float(r2_score(self.truth, self.predictions))


_encode_predictions_and_truth_ = False


def save_predictions_to_file(dataset: Dataset, output_file: str,
                             predictions=None, truth=None,
                             probabilities=None, probabilities_labels=None,
                             target_is_encoded=False):
    TaskResult.save_predictions(dataset, output_file=output_file,
                                predictions=predictions, truth=truth,
                                probabilities=probabilities, probabilities_labels=probabilities_labels,
                                target_is_encoded=target_is_encoded)
