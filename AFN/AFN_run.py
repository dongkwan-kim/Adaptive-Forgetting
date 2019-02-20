import tensorflow as tf

import AFN
from data import *

np.random.seed(1004)
flags = tf.app.flags
flags.DEFINE_integer("max_iter", 400, "Epoch to train")
flags.DEFINE_float("lr", 0.001, "Learing rate(init) for train")
flags.DEFINE_integer("batch_size", 256, "The size of batch for 1 iteration")
flags.DEFINE_string("checkpoint_dir", "./checkpoints", "Directory path to save the checkpoints")
flags.DEFINE_integer("dims0", 784, "Dimensions about input layer")
flags.DEFINE_integer("dims1", 64, "Dimensions about 1st layer")
flags.DEFINE_integer("dims2", 32, "Dimensions about 2nd layer")
flags.DEFINE_integer("dims3", 10, "Dimensions about output layer")
flags.DEFINE_integer("n_classes", 10, 'The number of classes at each task')
flags.DEFINE_float("l1_lambda", 0.00001, "Sparsity for L1")
flags.DEFINE_float("l2_lambda", 0.0001, "L2 lambda")
flags.DEFINE_float("gl_lambda", 0.001, "Group Lasso lambda")
flags.DEFINE_float("regular_lambda", 0.5, "regularization lambda")
flags.DEFINE_integer("ex_k", 10, "The number of units increased in the expansion processing")
flags.DEFINE_float('loss_thr', 0.1, "Threshold of dynamic expansion")
flags.DEFINE_float('spl_thr', 0.1, "Threshold of split and duplication")

# New hyper-parameters
flags.DEFINE_integer("n_tasks", 10, 'The number of tasks')
flags.DEFINE_integer("task_to_forget", 6, 'Task to forget')
flags.DEFINE_integer("one_step_neurons", 5, 'Number of neurons to forget in one step')
flags.DEFINE_integer("steps_to_forget", 25, 'Total number of steps in forgetting')

MODE = "SMALL"
if MODE == "TEST":
    flags.FLAGS.max_iter = 90
    flags.FLAGS.n_tasks = 2
    flags.FLAGS.task_to_forget = 1
    flags.FLAGS.steps_to_forget = 6
    flags.FLAGS.checkpoint_dir = "./checkpoints/test"
elif MODE == "SMALL":
    flags.FLAGS.max_iter = 200
    flags.FLAGS.n_tasks = 4
    flags.FLAGS.task_to_forget = 2
    flags.FLAGS.steps_to_forget = 14
    flags.FLAGS.checkpoint_dir = "./checkpoints/small"

FLAGS = flags.FLAGS


def experiment_forget(afn: AFN.AFN, flags, policies):
    for policy in policies:
        afn.sequentially_adaptive_forget_and_predict(
            flags.task_to_forget, flags.one_step_neurons, flags.steps_to_forget,
            policy=policy,
        )
        afn.recover_old_params()

    afn.print_summary(flags.task_to_forget, flags.one_step_neurons)
    afn.draw_chart_summary(flags.task_to_forget, flags.one_step_neurons,
                           file_prefix="task{}_step{}".format(flags.task_to_forget, flags.one_step_neurons))


def experiment_forget_and_retrain(afn: AFN.AFN, flags, policies, coreset=None):
    for policy in policies:
        afn.sequentially_adaptive_forget_and_predict(
            flags.task_to_forget, flags.one_step_neurons, flags.steps_to_forget,
            policy=policy,
        )
        afn.retrain_after_forgetting(flags, policy, coreset)
        afn.clear_experiments()


if __name__ == '__main__':

    mnist_data, train_xs, val_xs, test_xs = get_data_of_multiple_tasks(FLAGS.n_tasks)
    mnist_coreset = MNISTCoreset(mnist_data, train_xs, val_xs, test_xs, sampling_ratio=[0.2, 1.0, 1.0])

    model = AFN.AFN(FLAGS)
    model.add_dataset(mnist_data, train_xs, val_xs, test_xs)

    if not model.restore():
        model.train_den(FLAGS)
        model.get_importance_matrix()
        model.save()

    policies_for_expr = ["LIN", "EIN", "RANDOM", "ALL"]
    experiment_forget_and_retrain(model, FLAGS, policies_for_expr, mnist_coreset)