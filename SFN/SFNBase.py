from collections import defaultdict, OrderedDict
from typing import Dict, List, Callable, Tuple
import os
import pickle
import math

import numpy as np
import numpy.linalg as npl
import tensorflow as tf
from cges.cges import get_sparsity_of_variable
from termcolor import cprint
from tqdm import trange

from data import Coreset, DataLabel
from enums import UnitType
from utils import build_line_of_list, print_all_vars, get_zero_expanded_matrix, parse_var_name, get_available_gpu_names
from utils_importance import *


def get_utype_from_layer_type(layer_type: str or List[str]) -> UnitType or List[UnitType]:
    # hard coded conversion
    layer_type_to_utype = {
        "conv": UnitType.FILTER,
        "fc": UnitType.NEURON,
        "layer": UnitType.NEURON,  # TODO: Replace layer to fc
        "bn": UnitType.NONE,
        "mask": UnitType.NONE,
    }
    if isinstance(layer_type, str):
        return layer_type_to_utype[layer_type]
    elif isinstance(layer_type, list):
        return [layer_type_to_utype[lt] for lt in layer_type]
    else:
        raise TypeError("layer_type is type({})".format(type(layer_type)))


class SFN:

    def __init__(self, config):

        self.sess = None

        self.batch_size = config.batch_size
        self.checkpoint_dir = config.checkpoint_dir
        self.data_labels: DataLabel = None
        self.trainXs, self.valXs, self.testXs = None, None, None
        self.coreset: Coreset = None
        self.n_tasks = config.n_tasks
        self.dims = None

        self.importance_matrix_tuple = None
        self.importance_criteria = config.importance_criteria
        self.online_importance = config.online_importance

        self.gpu_names = get_available_gpu_names(config.gpu_num_list)
        self.gpu_num_list = config.gpu_num_list
        assert len(self.gpu_names) <= 1, "Not support multi-GPU, yet"

        self.prediction_history: Dict[str, List] = defaultdict(list)
        self.pruning_rate_history: Dict[str, List] = defaultdict(list)
        self.layer_to_removed_neuron_set: Dict[str, set] = defaultdict(set)
        self.layer_types: List[str] = []

        self.batch_idx = 0
        self.retrained = False

        self.params = {}
        self.sfn_params = {}
        self.old_params_list = []

        self.attr_to_save = [
            "importance_matrix_tuple",
            "importance_criteria",
            "old_params_list",
            "layer_to_removed_neuron_set",
            "n_tasks",
            "layer_types",
        ]

    def __repr__(self):
        return "{}_{}_{}".format(self.__class__.__name__, self.n_tasks, "_".join(map(str, self.dims)))

    def get_real_device_info(self) -> List[str]:
        if self.gpu_num_list:
            return ["gpu-{}".format(gn) for gn in self.gpu_num_list]
        else:
            return ["cpu"]

    def set_layer_types(self, *args, **kwargs):
        """Set self.layer_types, the list of types (prefix of scope) (e.g. layer, conv, fc, ...)"""
        raise NotImplementedError

    def add_dataset(self, data_labels, train_xs, val_xs, test_xs, coreset):
        self.data_labels, self.trainXs, self.valXs, self.testXs = data_labels, train_xs, val_xs, test_xs
        self.coreset = coreset

    def predict_only_after_training(self) -> list:
        raise NotImplementedError

    def initial_train(self, *args):
        raise NotImplementedError

    # Variable, params, ... attributes Manipulation

    def get_name_to_param_shapes(self) -> Dict[str, tuple]:
        return {name: tuple(param.get_shape().as_list()) for name, param in self.params.items()}

    def get_params(self) -> dict:
        raise NotImplementedError

    def load_params(self, params, *args, **kwargs):
        raise NotImplementedError

    def recover_params(self, idx):
        raise NotImplementedError

    def create_variable(self, scope, name, shape, trainable=True) -> tf.Variable:
        raise NotImplementedError

    def get_variable(self, scope, name, trainable=True) -> tf.Variable:
        raise NotImplementedError

    def assign_new_session(self, idx_to_load_params=None):
        """
        :param idx_to_load_params: if idx_to_load_params is None, use current params
                                   else use self.old_params_list[idx_to_load_params]
        :return:
        """
        raise NotImplementedError

    def get_removed_neurons_of_scope(self, scope, **kwargs) -> list:
        return [neuron for neuron in self.layer_to_removed_neuron_set[scope]]

    def clear(self):
        tf.reset_default_graph()
        self.sess.close()

    def sfn_create_variable(self, scope, name, shape=None, trainable=True, initializer=None):
        with tf.variable_scope(scope):
            w = tf.get_variable(name, shape, initializer=initializer, trainable=trainable)
            if 'new' not in w.name:
                self.sfn_params[w.name] = w
        return w

    def sfn_get_variable(self, scope, name, trainable=True):
        with tf.variable_scope(scope, reuse=True):
            w = tf.get_variable(name, trainable=trainable)
            self.sfn_params[w.name] = w
        return w

    def sfn_create_or_get_variable(self, scope, name, shape=None, trainable=True, initializer=None):
        try:
            w = self.sfn_create_variable(scope, name, shape, trainable, initializer)
        except ValueError:
            w = self.sfn_get_variable(scope, name, trainable)
        return w

    def sfn_get_params(self, name_filter: Callable = None):
        """ Access the sfn_parameters """
        mdict = dict()
        for scope_name, param in self.sfn_params.items():
            if name_filter is None or name_filter(scope_name):
                w = self.sess.run(param)
                mdict[scope_name] = w
        return mdict

    # Save & Restore

    def save(self, model_name=None, model_middle_path=None):
        model_name = model_name or str(self)
        checkpoint_dir = os.path.join(self.checkpoint_dir, model_middle_path or "")
        model_path = os.path.join(checkpoint_dir, "{}.ckpt".format(model_name))

        # Model Save
        saver = tf.train.Saver()
        saver.save(self.sess, model_path)
        print_all_vars("Saved: {}".format(model_path), "blue")

        # Attribute Save
        self.save_attr(model_name, model_middle_path=model_middle_path)

    def save_attr(self, model_name=None, attr=None, model_middle_path=None):
        model_name = model_name or str(self)
        checkpoint_dir = os.path.join(self.checkpoint_dir, model_middle_path or "")
        attr_path = os.path.join(checkpoint_dir, "{}_attr.pkl".format(model_name))
        attr = attr or self.attr_to_save
        with open(attr_path, "wb") as f:
            pickle.dump({k: self.__dict__[k] for k in attr}, f)
        cprint("Saved: attribute of {}".format(model_name), "blue")
        for a in attr:
            print("\t - {}".format(a))

    def restore(self, model_name=None, model_middle_path=None, build_model=False) -> bool:
        model_name = model_name or str(self)
        checkpoint_dir = os.path.join(self.checkpoint_dir, model_middle_path or "")
        model_path = os.path.join(checkpoint_dir, "{}.ckpt".format(model_name))

        if not os.path.isfile("{}.meta".format(model_path)):
            return False

        try:
            # Attribute Restore
            self.restore_attr(model_name, model_middle_path)

            # Recreate variables
            self.create_model_variables()
            if build_model:
                self.build_model()

            # Model Restore
            self.sess = tf.Session()
            self.sess.run(tf.global_variables_initializer())
            saver = tf.train.Saver()
            saver.restore(self.sess, tf.train.latest_checkpoint(checkpoint_dir))
            print_all_vars("Restored: {}".format(model_path), "blue")
            return True

        except Exception as e:
            cprint("Restore Failed: {}\n\t{}".format(model_path, str(e)), "red")
            return False

    def restore_attr(self, model_name=None, model_middle_path=None):
        model_name = model_name or str(self)
        checkpoint_dir = os.path.join(self.checkpoint_dir, model_middle_path or "")
        attr_path = os.path.join(checkpoint_dir, "{}_attr.pkl".format(model_name))
        with open(attr_path, "rb") as f:
            attr_dict: dict = pickle.load(f)
            self.__dict__.update(attr_dict)
        cprint("Restored: attribute of {}".format(model_name), "blue")
        for a in attr_dict.keys():
            print("\t - {}".format(a))

    def create_model_variables(self):
        raise NotImplementedError

    def build_model(self):
        raise NotImplementedError

    # Data batch

    def initialize_batch(self):
        self.batch_idx = 0

    def get_next_batch(self, x, y, batch_size=None):
        batch_size = batch_size if batch_size else self.batch_size
        next_idx = self.batch_idx + batch_size
        r = x[self.batch_idx:next_idx], y[self.batch_idx:next_idx]
        self.batch_idx = next_idx
        return r

    # Data visualization

    def print_summary(self, task_id):
        for policy, history in self.prediction_history.items():
            print("== {} ==".format(policy))
            print("\t".join(
                ["Pruning rate"]
                + [str(x) for x in range(1, len(history[0]) + 1)]
                + ["AUROC-{}".format(policy)]
            ))
            pruning_rate_as_x = self.pruning_rate_history[policy]
            for i, (pruning_rate, perf) in enumerate(zip(pruning_rate_as_x, history)):
                perf_except_t = np.delete(perf, task_id - 1)
                mean_perf = np.mean(perf_except_t)
                print("\t".join([str(pruning_rate)] + [str(x) for x in perf] + [str(mean_perf)]))

    def draw_chart_summary(self, task_id_or_ids,
                           file_prefix=None, file_extension=".png", ylim=None, highlight_ylabels=None,
                           **kwargs):

        mean_perf_except_t = None
        min_perf_except_t = None
        max_decline_except_t = None
        task_id_list = [task_id_or_ids] if isinstance(task_id_or_ids, int) else task_id_or_ids

        for policy, history in self.prediction_history.items():

            pruning_rate_as_x = self.pruning_rate_history[policy]
            history_txn = np.transpose(history)
            tasks = [x for x in range(1, self.n_tasks + 1)]

            build_line_of_list(x_or_x_list=pruning_rate_as_x,
                               is_x_list=False,
                               y_list=history_txn,
                               label_y_list=tasks,
                               xlabel="Pruning rate", ylabel="AUROC",
                               ylim=ylim or [0.5, 1],
                               title="AUROC by {} Neuron Deletion".format(policy),
                               file_name="{}_{}_{}{}".format(
                                   file_prefix, self.importance_criteria.split("_")[0], policy, file_extension),
                               highlight_ylabels=task_id_list,
                               **kwargs)

            history_txn_except_t = np.delete(history_txn, [tid - 1 for tid in task_id_list], axis=0)
            history_n_mean_except_t = np.mean(history_txn_except_t, axis=0)
            history_n_min_except_t = np.min(history_txn_except_t, axis=0)
            history_n_max_decline_except_t = np.max(history_txn_except_t[:, [0]] - history_txn_except_t, axis=0)

            if mean_perf_except_t is None:
                mean_perf_except_t = history_n_mean_except_t
                min_perf_except_t = history_n_min_except_t
                max_decline_except_t = history_n_max_decline_except_t
            else:
                mean_perf_except_t = np.vstack((mean_perf_except_t, history_n_mean_except_t))
                min_perf_except_t = np.vstack((min_perf_except_t, history_n_min_except_t))
                max_decline_except_t = np.vstack((max_decline_except_t, history_n_max_decline_except_t))

        policy_keys = [policy for policy in self.prediction_history.keys()]
        pruning_rate_as_x_list = [self.pruning_rate_history[policy] for policy in policy_keys]

        build_line_of_list(x_or_x_list=pruning_rate_as_x_list,
                           y_list=mean_perf_except_t,
                           label_y_list=policy_keys,
                           xlabel="Pruning rate", ylabel="Mean of Average Per-task AUROC",
                           ylim=ylim or [0.5, 1],
                           title="Mean Perf. Without Task-{}".format(task_id_list),
                           file_name="{}_{}_MeanAcc{}".format(
                               file_prefix, self.importance_criteria.split("_")[0], file_extension),
                           highlight_ylabels=highlight_ylabels,
                           **kwargs)
        build_line_of_list(x_or_x_list=pruning_rate_as_x_list,
                           y_list=max_decline_except_t,
                           label_y_list=policy_keys,
                           xlabel="Pruning rate", ylabel="Max of decline Per-task AUROC",
                           ylim=ylim or [-0.05, 0.5],
                           title="Max Decline of Perf. Without Task-{}".format(task_id_list),
                           file_name="{}_{}_MaxDecline{}".format(
                               file_prefix, self.importance_criteria.split("_")[0], file_extension),
                           highlight_ylabels=highlight_ylabels,
                           **kwargs)
        build_line_of_list(x_or_x_list=pruning_rate_as_x_list,
                           y_list=min_perf_except_t,
                           label_y_list=policy_keys,
                           xlabel="Pruning rate", ylabel="Min of Average Per-task AUROC",
                           ylim=ylim or [0.5, 1],
                           title="Minimum Perf. Without Task-{}".format(task_id_list),
                           file_name="{}_{}_MinAcc{}".format(
                               file_prefix, self.importance_criteria.split("_")[0], file_extension),
                           highlight_ylabels=highlight_ylabels,
                           **kwargs)

    # Utils for sequential experiments

    def clear_experiments(self):
        self.layer_to_removed_neuron_set = defaultdict(set)
        self.recover_old_params()

    def recover_recent_params(self):
        print("\n RECOVER RECENT PARAMS")
        self.recover_params(-1)

    def recover_old_params(self):
        print("\n RECOVER OLD PARAMS")
        self.recover_params(0)

    # Pruning strategies

    def _get_selected_by_layers(self, selected_indices: np.ndarray) -> tuple:
        """
        Convert selected_indices of whole ndarray to indices of layer.
        """
        selected_by_layers_list = []
        prev_divider = 0
        for i_mat in self.importance_matrix_tuple:
            divider = prev_divider + i_mat.shape[-1]
            selected_by_layers_list.append(
                selected_indices[(selected_indices >= prev_divider) &
                                 (selected_indices < divider)]
                - prev_divider
            )
            prev_divider = divider
        return tuple(selected_by_layers_list)

    def _get_indices_of_certain_utype(self, ordered_indices: np.ndarray, certain_utype) -> np.ndarray:

        # [<UnitType.FILTER: 1>, <UnitType.FILTER: 1>, ... <UnitType.NEURON: 0>]
        utypes_of_layers = get_utype_from_layer_type(self.layer_types)

        assert len(utypes_of_layers) - 1 == len(self.importance_matrix_tuple), \
            "{} - 1 != {}".format(len(utypes_of_layers), len(self.importance_matrix_tuple))
        assert certain_utype in utypes_of_layers

        if len(set(utypes_of_layers)) == 1:
            return ordered_indices

        elif len(set(utypes_of_layers)) >= 2:
            utypes_to_num_units = OrderedDict()
            utype_to_loop = None
            for utype, i_mat_of_layer in zip(utypes_of_layers, self.importance_matrix_tuple):
                if utype_to_loop != utype:
                    utype_to_loop = utype
                    utypes_to_num_units[utype_to_loop] = 0
                # i_mat_of_layer.shape[-1]: -1 of (n_tasks, n_units/layer)
                utypes_to_num_units[utype_to_loop] += i_mat_of_layer.shape[-1]

            start_idx = 0
            for i, utype in enumerate(utypes_to_num_units):

                utype_start, utype_end = (start_idx, start_idx + utypes_to_num_units[utype])
                start_idx = utypes_to_num_units[utype]

                # utype_start <= X < utype_end, when X is indices of utype can have.
                if certain_utype == utype:
                    return ordered_indices[(ordered_indices >= utype_start) & (ordered_indices < utype_end)]
        else:
            raise ValueError("len(set(utypes_of_layers)) should be not 0")

    def _get_reduced_i_mat(self, task_id_or_ids, use_complementary_tasks: bool = False):
        i_mat = np.concatenate(self.importance_matrix_tuple, axis=1)

        if use_complementary_tasks:
            n_tasks, _ = i_mat.shape
            _task_ids = [task_id_or_ids] if isinstance(task_id_or_ids, int) else task_id_or_ids

            # Complementary tasks
            task_id_or_ids = [tid for tid in range(1, n_tasks + 1) if tid not in _task_ids]

        if isinstance(task_id_or_ids, int):
            i_mat = np.delete(i_mat, task_id_or_ids - 1, axis=0)
        elif isinstance(task_id_or_ids, list):
            i_mat = np.delete(i_mat, [tid - 1 for tid in task_id_or_ids], axis=0)
        else:
            raise TypeError
        return i_mat

    def get_mean_importance(self, task_id_or_ids) -> np.ndarray:
        i_mat = self._get_reduced_i_mat(task_id_or_ids)
        li = np.mean(i_mat, axis=0)
        return li

    def get_maximum_importance(self, task_id_or_ids) -> np.ndarray:
        i_mat = self._get_reduced_i_mat(task_id_or_ids)
        return np.max(i_mat, axis=0)

    def get_importance_task_related_deviation(self, task_id_or_ids, relatedness_type: str, tau: float) -> np.ndarray:

        i_mat_to_remember = self._get_reduced_i_mat(task_id_or_ids)  # (T - |S|, |H|)
        mean_i_mat_to_remember = np.mean(i_mat_to_remember, axis=0)  # (|H|,)
        n_units = len(mean_i_mat_to_remember)
        deviation_i_mat_to_remember = np.abs(i_mat_to_remember - mean_i_mat_to_remember)  # (T - |S|, |H|)

        i_mat_to_forget = self._get_reduced_i_mat(task_id_or_ids, use_complementary_tasks=True)  # (|S|, |H|)
        mean_i_mat_to_forget = np.mean(i_mat_to_forget, axis=0)  # (|H|,)

        _e = 1e-7

        if relatedness_type == "symmetric_task_level":
            rho = np.tanh(tau / (_e + npl.norm(i_mat_to_remember - mean_i_mat_to_forget, axis=1)))  # (T - |S|,)
            rho = np.transpose(np.tile(rho, (n_units, 1)))  # (T - |S|, |H|)

        elif relatedness_type == "symmetric_unit_level":
            rho = np.tanh(tau / (_e + np.abs(i_mat_to_remember - mean_i_mat_to_forget)))  # (T - |S|, |H|)

        elif relatedness_type == "asymmetric_unit_level":
            rho = np.tanh(tau * np.abs(i_mat_to_remember)
                          / (_e + np.abs(i_mat_to_remember - mean_i_mat_to_forget)))  # (T - |S|, |H|)

        elif relatedness_type == "constant":
            rho = np.ones(i_mat_to_remember.shape)

        else:
            raise ValueError("{} does not have an appropriate relatedness_type".format(relatedness_type))

        if relatedness_type != "constant":
            cprint("rho: {} +- {}".format(np.mean(rho), np.std(rho)), "red")
            assert 0.3 < np.mean(rho) < 0.7, "np.mean(rho) = {}".format(np.mean(rho))

        related_deviation = np.mean(rho * deviation_i_mat_to_remember, axis=0)  # (T - |S|, |H|) -> (|H|,)
        assert related_deviation.shape == (n_units,), \
            "related_deviation.shape, {}, is not ({},)".format(related_deviation.shape, n_units)
        return related_deviation

    def get_units_with_task_related_deviation(self, task_id_or_ids, number_to_select, utype,
                                              mixing_coeff, relatedness_type, tau):
        mean_i = self.get_mean_importance(task_id_or_ids)

        if mixing_coeff > 0:
            related_deviation = self.get_importance_task_related_deviation(task_id_or_ids, relatedness_type, tau)
            deviated = mixing_coeff * related_deviation + (1 - mixing_coeff) * mean_i
        else:
            deviated = mean_i

        deviated_asc_sorted_idx = self._get_indices_of_certain_utype(np.argsort(deviated), utype)
        selected = deviated_asc_sorted_idx[:number_to_select]
        return self._get_selected_by_layers(selected)

    def get_units_by_maximum_importance(self, task_id_or_ids, number_to_select, utype):
        """Pruning ConvNets Online for Efficient Specialist Models, CVPR W, 2018."""
        max_i = self.get_maximum_importance(task_id_or_ids)
        maximized_asc_sorted_idx = self._get_indices_of_certain_utype(np.argsort(max_i), utype)
        selected = maximized_asc_sorted_idx[:number_to_select]
        return self._get_selected_by_layers(selected)

    def get_units_by_mean_importance(self, task_id_or_ids, number_to_select, utype):
        """
        Pruning Filters and Classes: Towards On-Device Customization of Convolutional Neural Networks, Mobisys W, 2017.
        Recovering from Random Pruning: On the Plasticity of Deep Convolutional Neural Networks, Arxiv, 2018.
        """
        mean_i = self.get_mean_importance(task_id_or_ids)
        asc_sorted_idx = self._get_indices_of_certain_utype(np.argsort(mean_i), utype)
        selected = asc_sorted_idx[:number_to_select]
        return self._get_selected_by_layers(selected)

    def get_random_units(self, number_to_select, utype):
        i_mat = np.concatenate(self.importance_matrix_tuple, axis=1)
        indexes = np.asarray(range(i_mat.shape[-1]))
        np.random.seed(i_mat.shape[-1])
        np.random.shuffle(indexes)
        indexes = self._get_indices_of_certain_utype(indexes, utype)
        selected = indexes[:number_to_select]
        return self._get_selected_by_layers(selected)

    # Selective forgetting

    def selective_forget(self, task_to_forget, number_of_units, policy, utype, **kwargs) -> Tuple[np.ndarray]:

        self.old_params_list.append(self.get_params())

        if not self.importance_matrix_tuple:
            self.get_importance_matrix()

        cprint("\n SELECTIVE FORGET {} task_id-{} from {}, {} {}".format(
            policy, task_to_forget, self.n_tasks, number_of_units, utype), "green")

        policy = policy.split(":")[0]
        if policy == "OURS":
            unit_indexes = self.get_units_with_task_related_deviation(task_to_forget, number_of_units, utype, **kwargs)
        elif policy == "MAX":
            unit_indexes = self.get_units_by_maximum_importance(task_to_forget, number_of_units, utype)
        elif policy == "MEAN":
            unit_indexes = self.get_units_by_mean_importance(task_to_forget, number_of_units, utype)
        elif policy == "CONST":
            unit_indexes = self.get_units_with_task_related_deviation(task_to_forget, number_of_units, utype,
                                                                      relatedness_type="constant", **kwargs)
        elif policy == "RANDOM":
            unit_indexes = self.get_random_units(number_of_units, utype)
        elif policy == "ALL_MEAN":
            unit_indexes = self.get_units_with_task_related_deviation([], number_of_units, utype,
                                                                      mixing_coeff=0,
                                                                      relatedness_type="constant",
                                                                      tau=0)
        elif policy == "ALL_CONST":
            unit_indexes = self.get_units_with_task_related_deviation([], number_of_units, utype,
                                                                      relatedness_type="constant", **kwargs)
        else:
            raise NotImplementedError

        # e.g. ['conv', 'conv', 'fc', 'fc', 'fc'] -> ["conv0", "conv1", "fc0", "fc1", "fc2"]
        scope_list = self._get_scope_list()

        for scope, ni in zip(scope_list, unit_indexes):
            self._remove_pruning_units(scope, ni)

        self.assign_new_session()

        return unit_indexes

    def _remove_pruning_units(self, scope, indexes: np.ndarray):
        """Zeroing columns of target indexes"""

        if len(indexes) == 0:
            return

        print(" REMOVE NEURONS {} ({})".format(scope, len(indexes)))
        self.layer_to_removed_neuron_set[scope].update(set(indexes))

        w: tf.Variable = self.get_variable(scope, "weight", False)
        b: tf.Variable = self.get_variable(scope, "biases", False)

        val_w = w.eval(session=self.sess)
        val_b = b.eval(session=self.sess)

        for i in indexes:
            if len(val_w.shape) == 2:  # fc layer
                val_w[:, i] = 0
            elif len(val_w.shape) == 4:  # conv2d layer
                val_w[:, :, :, i] = 0
            val_b[i] = 0

        self.sess.run(tf.assign(w, val_w))
        self.sess.run(tf.assign(b, val_b))

        self.params[w.name] = w
        self.params[b.name] = b

    def sequentially_selective_forget_and_predict(self,
                                                  task_to_forget: list or int,
                                                  utype_to_one_step_units: dict,
                                                  steps_to_forget,
                                                  policy,
                                                  params_of_utype: dict,
                                                  fast_skip: bool = False):

        cprint("\n SEQUENTIALLY SELECTIVE FORGET {} task_id-{} from {}".format(
            policy, task_to_forget, self.n_tasks), "green")
        for utype, one_step_units in utype_to_one_step_units.items():
            cprint("\t {}: total {}".format(utype, one_step_units * steps_to_forget), "green")

        for i in range(steps_to_forget + 1):

            if fast_skip and i < 0.5 * (steps_to_forget + 1) and i % 4 != 0:
                cprint("Fast Skipped: {}/{} in policy {}".format(i, steps_to_forget + 1, policy), "red")
                continue

            list_of_unit_indices_by_layer = []
            for utype, one_step_units in utype_to_one_step_units.items():
                kwargs = {} if params_of_utype is None else params_of_utype[str(utype)]
                unit_indices_by_layer = self.selective_forget(
                    task_to_forget, i * one_step_units, policy, utype, **kwargs)
                list_of_unit_indices_by_layer.append(unit_indices_by_layer)

            pruning_rate = self.get_parameter_level_pruning_rate(
                list_of_unit_indices_by_layer,
                list(utype_to_one_step_units.keys()))
            self.pruning_rate_history[policy].append(pruning_rate)

            pred = self.predict_only_after_training()
            self.prediction_history[policy].append(pred)

            if i != steps_to_forget:
                self.recover_recent_params()

    def _get_scope_postfixes(self) -> List[int]:
        """
        :return: conversion of self.layer_types
        e.g. self.layer_types ['conv', 'conv', 'fc', 'fc', 'fc'] -> [0, 1, 0, 1, 2]
        """
        scope_postfixes = []
        scope_counted = {scope_prefix: 0 for scope_prefix in set(self.layer_types)}
        for scope_prefix in self.layer_types:
            scope_postfixes.append(scope_counted[scope_prefix])
            scope_counted[scope_prefix] += 1
        return scope_postfixes

    def _get_scope_list(self) -> List[str]:
        """
        :return: conversion of self.layer_types
        e.g. ['conv', 'conv', 'fc', 'fc', 'fc'] -> ["conv1", "conv2", "fc1", "fc2", "fc3"]
        """
        return ["{}{}".format(layer_type, postfix + 1)
                for layer_type, postfix in zip(self.layer_types, self._get_scope_postfixes())]

    def get_parameter_level_pruning_rate(self,
                                         list_of_unit_indices_by_layer: List[Tuple[np.ndarray]],
                                         utype_list: List[UnitType]) -> float:
        """
        :param list_of_unit_indices_by_layer: List[Tuple[np.ndarray]]
        :param utype_list: List[UnitType] of interest
        :return: num_total_pruned_parameters / num_total_parameters
        """
        name_to_param_shapes = self.get_name_to_param_shapes()

        num_total_parameters = 0
        for name, param_shape in name_to_param_shapes.items():
            layer_type = "".join(c for c in name.split("/")[0] if not c.isdigit())
            unit_type = get_utype_from_layer_type(layer_type)
            if unit_type in utype_list:
                num_total_parameters += np.prod(param_shape)

        num_total_pruned_parameters = 0
        for tuple_of_unit_indices_of_layer, scope in zip(zip(*list_of_unit_indices_by_layer),
                                                         self._get_scope_list()):
            # ndarray of shape (n_units/layer,)
            unit_indices_of_layer = np.concatenate(tuple_of_unit_indices_of_layer)
            num_pruned_unit = len(unit_indices_of_layer)

            weight_shape = name_to_param_shapes["{}/weight:0".format(scope)]
            # e.g.
            # if weight shape = (11, 11, 3, 96), 11 * 11 * 3 * num_pruned_unit (weight) + num_pruned_unit (biases)
            # if weight shape = (4096, 1024), 4096 * num_pruned_unit (weight) + num_pruned_unit (biases)
            pruned_parameters_at_this_layer = np.prod(weight_shape[:-1]) * num_pruned_unit + num_pruned_unit
            num_total_pruned_parameters += pruned_parameters_at_this_layer

        return float(num_total_pruned_parameters / num_total_parameters)

    # Importance vectors

    def get_importance_vector(self, task_id, importance_criteria: str,
                              layer_separate=False, use_coreset=False) -> tuple or np.ndarray:
        """
        :param task_id:
        :param importance_criteria:
        :param layer_separate:
            - layer_separate = True: tuple of ndarray of shape (|h1|,), (|h2|,) or
            - layer_separate = False: ndarray of shape (|h|,)
        :param use_coreset: Whether use coreset in computing IV.

        First construct the model, then pass tf vars to get_importance_vector_from_tf_vars

        :return: The return value of get_importance_vector_from_tf_vars
        """
        raise NotImplementedError

    def get_importance_vector_from_tf_vars(self, task_id, importance_criteria,
                                           h_length_list, hidden_layer_list, gradient_list, weight_list, bias_list,
                                           X, Y,
                                           layer_separate=False, use_coreset=False) -> tuple or np.ndarray:

        importance_vectors = [np.zeros(shape=(0, h_length)) for h_length in h_length_list]

        if use_coreset:
            xs, ys, _, _, _, _ = self.coreset[task_id - 1]
        else:
            xs, ys = self.trainXs[task_id - 1], self.data_labels.get_train_labels(task_id)

        self.initialize_batch()
        num_batches = int(math.ceil(len(xs) / self.batch_size))
        for _ in trange(num_batches):

            batch_x, batch_y = self.get_next_batch(xs, ys)

            # shape = (batch_size, |h|)
            if importance_criteria == "first_Taylor_approximation":
                batch_importance_vectors = get_1st_taylor_approximation_based(self.sess, {
                    "hidden_layers": hidden_layer_list,
                    "gradients": gradient_list,
                }, {X: batch_x, Y: batch_y})

            elif importance_criteria == "activation":
                batch_importance_vectors = get_activation_based(self.sess, {
                    "hidden_layers": hidden_layer_list,
                }, {X: batch_x, Y: batch_y})

            elif importance_criteria == "magnitude":
                batch_importance_vectors = get_magnitude_based(self.sess, {
                    "weights": weight_list,
                    "biases": bias_list,
                }, {X: batch_x, Y: batch_y})

            elif importance_criteria == "gradient":
                batch_importance_vectors = get_gradient_based(self.sess, {
                    "gradients": gradient_list,
                }, {X: batch_x, Y: batch_y})

            else:
                raise NotImplementedError

            # importance_vectors[i].shape = (\sum N_{batch-1}, |h|)
            # if h is fc:
            #   batch_i_vector.shape = (N_{batch}, |h|)
            # elif h is conv:
            #   batch_i_vector.shape = (N_{batch}, ksz, ksz, |h|)
            for i, batch_i_vector in enumerate(batch_importance_vectors):
                if len(batch_i_vector.shape) == 2:  # fc
                    importance_vectors[i] = np.vstack((importance_vectors[i], batch_i_vector))
                elif len(batch_i_vector.shape) == 4:  # conv2d
                    reduced_i_vector = np.mean(batch_i_vector, axis=(1, 2))
                    importance_vectors[i] = np.vstack((importance_vectors[i], reduced_i_vector))
                else:
                    raise ValueError("i_vector.shape is 2 or 4")

        for i in range(len(importance_vectors)):
            importance_vectors[i] = importance_vectors[i].sum(axis=0)

        if layer_separate:
            return tuple(importance_vectors)  # (|h1|,), (|h2|,)
        else:
            return np.concatenate(importance_vectors)  # shape = (|h|,)

    # shape = (T, |h|) or (T, |h1|), (T, |h2|)
    def get_importance_matrix(self, layer_separate=False, importance_criteria=None, use_coreset=False):

        importance_matrices = []

        importance_criteria = importance_criteria or self.importance_criteria
        self.importance_criteria = importance_criteria

        for t in reversed(range(1, self.n_tasks + 1)):
            i_vector_tuple = self.get_importance_vector(
                task_id=t,
                layer_separate=True,
                importance_criteria=importance_criteria,
                use_coreset=use_coreset,
            )

            if t == self.n_tasks:
                for iv in i_vector_tuple:
                    importance_matrices.append(np.zeros(shape=(0, iv.shape[0])))

            for i, iv in enumerate(i_vector_tuple):
                imat = importance_matrices[i]
                importance_matrices[i] = np.vstack((
                    np.pad(iv, (0, imat.shape[-1] - iv.shape[0]), 'constant', constant_values=(0, 0)),
                    imat,
                ))

        self.importance_matrix_tuple = tuple(importance_matrices)
        if layer_separate:
            return self.importance_matrix_tuple  # (T, |h1|), (T, |h2|)
        else:
            return np.concatenate(self.importance_matrix_tuple, axis=1)  # shape = (T, |h|)

    def save_online_importance_matrix(self, task_id, importance_criteria=None):

        importance_criteria = importance_criteria or self.importance_criteria
        self.importance_criteria = importance_criteria

        i_vector_tuple = self.get_importance_vector(
            task_id=task_id,
            layer_separate=True,
            importance_criteria=importance_criteria,
            use_coreset=False,
        )

        if self.importance_matrix_tuple is None:
            self.importance_matrix_tuple = i_vector_tuple
        else:
            importance_matrices = []
            for i, iv in enumerate(i_vector_tuple):
                imat = self.importance_matrix_tuple[i]
                if len(imat.shape) == 1:
                    pad_width = (0, iv.shape[0] - imat.shape[-1])
                else:
                    pad_width = ((0, 0), (0, iv.shape[0] - imat.shape[-1]))
                new_imat = np.vstack((
                    np.pad(imat, pad_width, "constant", constant_values=(0, 0)),
                    iv,
                ))
                importance_matrices.append(new_imat)
            self.importance_matrix_tuple = tuple(importance_matrices)

    def normalize_importance_matrix_about_task(self):
        # TODO: filter norm / neuron norm
        i_mat = np.concatenate(self.importance_matrix_tuple, axis=1)  # (T, |H|)
        i_norm = npl.norm(i_mat, axis=1, keepdims=True)  # (T, 1)
        normalized_importance_matrices = []
        for i_mat_of_layer in self.importance_matrix_tuple:
            normalized_importance_matrices.append(i_mat_of_layer / i_norm)  # (T, |hx|)
        self.importance_matrix_tuple = tuple(normalized_importance_matrices)
        return self.importance_matrix_tuple

    def pprint_importance_matrix(self):
        for i_vec in np.concatenate(self.importance_matrix_tuple, axis=1):
            print("\t".join(str(importance) for importance in i_vec))

    def get_area_under_forgetting_curve(self, task_id_or_ids, policy_name, ratio=0.4):
        task_id_list = [task_id_or_ids] if isinstance(task_id_or_ids, int) else task_id_or_ids

        # Ys
        history = self.prediction_history[policy_name]
        history_txn = np.transpose(history)
        history_txn_except_t = np.delete(history_txn, [tid - 1 for tid in task_id_list], axis=0)
        history_n_mean_except_t = np.mean(history_txn_except_t, axis=0)
        history_n_min_except_t = np.min(history_txn_except_t, axis=0)

        # Xs
        pruning_rate_as_x = self.pruning_rate_history[policy_name]

        au_mean_fc = np.trapz(history_n_mean_except_t, x=pruning_rate_as_x)
        au_min_fc = np.trapz(history_n_min_except_t, x=pruning_rate_as_x)

        return au_mean_fc * ratio + au_min_fc * (1 - ratio)

    # Retrain after forgetting

    def retrain_after_forgetting(self, flags, policy, epoches_to_print: list = None, is_verbose: bool = True):
        cprint("\n RETRAIN AFTER FORGETTING - {}".format(policy), "green")
        self.retrained = True
        series_of_perfs = []

        # First, append perfs wo/ retraining
        perfs = self.predict_only_after_training()
        series_of_perfs.append(perfs)

        for retrain_iter in range(flags.retrain_task_iter):

            cprint("\n\n\tRE-TRAINING at iteration %d\n" % retrain_iter, "green")

            for t in range(flags.n_tasks):
                if (t + 1) != flags.task_to_forget:
                    coreset_t = self.coreset[t] if self.coreset is not None \
                        else (self.trainXs[t], self.data_labels.get_train_labels(t + 1),
                              self.valXs[t], self.data_labels.get_validation_labels(t + 1),
                              self.testXs[t], self.data_labels.get_test_labels(t + 1))
                    if is_verbose:
                        cprint("\n\n\tTASK %d RE-TRAINING at iteration %d\n" % (t + 1, retrain_iter), "green")
                    self._retrain_at_task(t + 1, coreset_t, flags, is_verbose)
                    self._assign_retrained_value_to_tensor(t + 1)
                    self.assign_new_session()
                else:
                    if is_verbose:
                        cprint("\n\n\tTASK %d NO NEED TO RE-TRAIN at iteration %d" % (t + 1, retrain_iter), "green")

            perfs = self.predict_only_after_training()
            series_of_perfs.append(perfs)

        if epoches_to_print:
            print("\t".join(str(t + 1) for t in range(self.n_tasks)))
            for epo in epoches_to_print:
                print("\t".join(str(round(perf, 4)) for perf in series_of_perfs[epo]))

        return series_of_perfs

    def get_retraining_vars_from_old_vars(self, scope: str,
                                          weight_name: str, bias_name: str,
                                          task_id: int, var_prefix: str,
                                          weight_var=None, bias_var=None, **kwargs) -> tuple:

        """
        :param scope: scope of variables.
        :param weight_name: if weight_variable is None, use get_variable else use it.
        :param bias_name: if bias_variable is None, use get_variable else use it.
        :param task_id: id of task / 1 ~
        :param var_prefix: variable prefix
        :param weight_var: weight_variable
        :param bias_var: bias_variable
        :param kwargs: kwargs for get_removed_neurons_of_scope
        :return: tuple of w and b

        e.g. when scope == "layer2",
        layer-"layer2", weight whose shape is (r0, c0), bias whose shape is (b0,)
        layer-next of "layer2", weight whose shape is (r1, c1), bias whose shape is (b1,)
        removed_neurons of (l-1)th [...] whose length is n0
        removed_neurons of (l)th [...] whose length is n1
        return weight whose shape is (r1 - n0, c1 - n1)
               bias whose shape is (b1 - n1)
        """
        scope_list = self._get_scope_list()
        scope_idx = scope_list.index(scope)
        prev_scope = scope_list[scope_idx - 1] if scope_idx > 0 else None

        assert self.importance_matrix_tuple is not None
        w: tf.Variable = self.get_variable(scope, weight_name, True) \
            if weight_var is None else weight_var
        b: tf.Variable = self.get_variable(scope, bias_name, True) \
            if bias_var is None else bias_var

        # TODO: CNN

        # Remove columns (neurons in the current layer)
        removed_neurons = self.get_removed_neurons_of_scope(scope, **kwargs)
        w: np.ndarray = np.delete(w.eval(session=self.sess), removed_neurons, axis=1)
        b: np.ndarray = np.delete(b.eval(session=self.sess), removed_neurons)

        # Remove rows (neurons in the previous layer)
        if prev_scope:  # Do not consider 1st layer, that does not have previous layer.
            removed_neurons_prev = self.get_removed_neurons_of_scope(prev_scope, **kwargs)
            w = np.delete(w, removed_neurons_prev, axis=0)

        sfn_w = self.sfn_create_or_get_variable("%s_t%d_%s" % (var_prefix, task_id, scope), weight_name,
                                                trainable=True, initializer=w)
        sfn_b = self.sfn_create_or_get_variable("%s_t%d_%s" % (var_prefix, task_id, scope), bias_name,
                                                trainable=True, initializer=b)
        return sfn_w, sfn_b

    def _retrain_at_task(self, task_id, data, retrain_flags, is_verbose):
        raise NotImplementedError

    def _assign_retrained_value_to_tensor(self, task_id, value_preprocess=None, **kwargs):

        # Get retrained values.
        retrained_values_dict = self.sfn_get_params(name_filter=lambda n: "_t{}_".format(task_id) in n
                                                                          and "retrain" in n)
        # get_zero_expanded_matrix.
        # TODO: CNN
        scope_list = self._get_scope_list()
        for name, retrained_value in list(retrained_values_dict.items()):
            prefix, task_id, scope, var_type = parse_var_name(name)
            scope_idx = scope_list.index(scope)
            prev_scope = scope_list[scope_idx - 1] if scope_idx > 0 else None
            removed_neurons = self.get_removed_neurons_of_scope(scope, **kwargs)

            # Expand columns
            retrained_value = get_zero_expanded_matrix(retrained_value, removed_neurons, add_rows=False)

            # Expand rows
            if prev_scope and "weight" in var_type:
                removed_neurons_prev = self.get_removed_neurons_of_scope(prev_scope, **kwargs)
                retrained_value = get_zero_expanded_matrix(retrained_value, removed_neurons_prev, add_rows=True)

            retrained_values_dict[name] = retrained_value

        # Assign values to tensors.
        for name, retrained_value in retrained_values_dict.items():
            prefix, task_id, scope, var_type = parse_var_name(name)
            tensor_sfn = self.params["{}/{}:0".format(scope, var_type)]
            value_sfn = tensor_sfn.eval(session=self.sess)

            if not value_preprocess:
                value_sfn = retrained_value
            else:
                value_sfn = value_preprocess(name, value_sfn, retrained_value)

            self.sess.run(tf.assign(tensor_sfn, value_sfn))

    def print_sparsity(self, iteration, variable_key="weight"):
        cprint("\n SPARSITY COMPUTATION at ITERATION {} on Devices {}".format(
            iteration, self.get_real_device_info()), "green")
        variable_to_be_sparsed = [v for v in tf.trainable_variables() if variable_key in v.name]
        tsp, sp_list = get_sparsity_of_variable(self.sess, variables=variable_to_be_sparsed)
        print("   [*] Total sparsity: {}".format(tsp))
        print("   [*] Sparsities:")
        for v, s in zip(variable_to_be_sparsed, sp_list):
            print("     - {}: {}".format(v.name, s))
