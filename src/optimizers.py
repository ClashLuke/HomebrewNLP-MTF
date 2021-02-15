"""
Stores custom optimizer classes as well as a custom optimizer creation utility as a handy wrapper
b"""
from __future__ import absolute_import, division, print_function

import typing

import mesh_tensorflow as mtf
import tensorflow.compat.v1 as tf

from .utils_mtf import weighted_add
from .dataclass import ModelParameter


def get_optimizer(mesh: mtf.Mesh, loss: mtf.Tensor, params: ModelParameter
                  ) -> typing.Tuple[mtf.Tensor, typing.List[mtf.Assign], typing.List[mtf.Tensor]]:
    """
    Creates optimizing and update/training operations.
    :param mesh: Mesh Tensorflow mesh
    :param loss: Final scalar loss of the model
    :param params: ModelParameter instance
    :return: scalar learning rate, update operations, gradients
    """
    global_step = tf.train.get_or_create_global_step()
    dtype = params.variable_dtype.activation_dtype
    learning_rate = tf.constant(value=params.learning_rate, shape=[], dtype=tf.float32)
    global_steps_float = tf.cast(global_step, tf.float32)
    # Cast to full precision
    learning_rate = tf.train.cosine_decay(learning_rate, global_step, params.train_steps, alpha=0.1)
    # Alpha is min learning_rate value as a fraction of init learning_rate.
    if params.warmup_steps > 0:
        warmup_steps_float = tf.constant(params.warmup_steps, dtype=tf.float32)
        is_warmup = tf.cast(global_steps_float < warmup_steps_float, tf.float32)
        learning_rate = (learning_rate * (is_warmup * global_steps_float / warmup_steps_float + (1 - is_warmup)))

    def _import_constant(name, x):
        return mtf.import_fully_replicated(mesh,
                                           tf.constant(x, dtype, []),
                                           mtf.Shape([]),
                                           name=name)

    learning_rate = mtf.import_fully_replicated(mesh, tf.cast(learning_rate, dtype), [], "learning_rate")
    beta1 = _import_constant("beta1", 0.9)
    beta2 = _import_constant("beta2", 0.95)
    mtf.scalar_summary("learning_rate", learning_rate)
    adam = Adam(params, learning_rate, params.weight_decay, beta1, beta2)
    if params.optimizer not in OPTIMIZERS:
        raise ValueError(f'Unknown optimizer "{params.optimizer}". Supported optimizers: {list(OPTIMIZERS.keys())}')
    optimizer = OPTIMIZERS[params.optimizer](params, learning_rate, params.weight_decay, beta1, beta2)
    clip_value = mtf.constant(mesh, params.gradient_clip, dtype=dtype)
    update_ops = []
    operations = loss.graph.operations
    xs = [x.outputs[0] for x in mesh.graph.trainable_variables]
    tensor_to_var = dict(zip(xs, mesh.graph.trainable_variables))
    loss_grad = mtf.Constant(loss.mesh, 1.0, loss.shape, loss.dtype).outputs[0]
    downstream = set(xs)
    for op in operations:
        if op.has_gradient and (set(op.inputs) & downstream):
            downstream |= set(op.outputs)
    tensor_to_gradient: typing.Dict[mtf.Tensor, typing.List[int, int, mtf.Tensor]] = {loss: [0, 0, loss_grad]}
    with tf.variable_scope(loss.graph.captured_variable_scope):
        for op in operations[::-1]:
            grad_outputs = []
            for out in op.outputs:
                grad = tensor_to_gradient.get(out)
                if grad is not None:
                    grad_outputs.append(grad[2])
                    grad[0] += 1
                else:
                    grad_outputs.append(None)
                if grad is not None and grad[0] == len(grad[2].operation.inputs):
                    del tensor_to_gradient[out]
            if not op.has_gradient or not any(grad_outputs) or not (set(op.inputs) & downstream):
                continue
            with tf.variable_scope(op.name + "/gradients"):
                for inp, grad in zip(op.inputs, op.gradient(grad_outputs)):
                    valid_grad = inp in downstream and grad is not None
                    if valid_grad and inp in tensor_to_gradient:
                        grad_list = tensor_to_gradient[inp]
                        grad_list[1] += 1
                        grad_list[2] += grad
                    elif valid_grad:
                        grad_list = [0, 1, grad]
                        tensor_to_gradient[inp] = grad_list
                    if valid_grad and len(inp.operation.outputs) == grad_list[1] and inp in tensor_to_var:
                        grad = grad_list[2]
                        var: mtf.Variable = tensor_to_var[inp]
                        optim = adam if var.shape.ndims == 0 else optimizer
                        norm = mtf.reduce_sum(mtf.square(grad)) / mtf.reduce_sum(mtf.square(var.value))
                        clipped = weighted_add(mtf.rsqrt(norm) * params.gradient_clip * grad, grad,
                                               mtf.cast(mtf.greater(mtf.sqrt(norm), params.gradient_clip), dtype))
                        weight_update, buffer = optim.apply_grad(clipped, var)
                        if params.weight_decay > 0:
                            weight_update += params.weight_decay * var.value
                        if var.shape.size > 1:
                            weight_update += mtf.reduce_mean(var.value)
                        update_ops.extend(buffer)
                        update_ops.append(mtf.assign_sub(var, weight_update))
    return mesh.graph.trainable_variables[0].graph.combine_assignments(update_ops)



def get_variable(params: ModelParameter, var, name, shape):
    return mtf.get_variable(var.mesh, name, shape,
                            initializer=tf.zeros_initializer(), trainable=False, dtype=params.variable_dtype)


class Optimizer(mtf.optimize.Optimizer):
    def __init__(self,
                 params: ModelParameter,
                 learning_rate,
                 weight_decay_rate=0.0,
                 beta_1=0.9,
                 beta_2=0.999,
                 epsilon=1e-5):
        self.params = params
        self.learning_rate = learning_rate
        self.beta1 = beta_1
        self.beta2 = beta_2
        self.epsilon = epsilon
        self.global_step = mtf.import_fully_replicated(params.mesh,
                                                       tf.cast(tf.train.get_or_create_global_step(),
                                                               params.calculation_dtype),
                                                       [], "global_steps_float")
        self.variable = lambda x, y, z: get_variable(params, x, f"{x.name}/{params.optimizer}/{y}", z)


class Adam(Optimizer):
    """A basic Adam optimizer that includes "correct" L2 weight decay."""

    def apply_grad(self, grad, var):
        """See base class."""
        exp_avg_p1_ptr = self.variable(var, 'exp_avg_p1', var.shape)
        exp_avg_p2_ptr = self.variable(var, 'exp_avg_p2', var.shape)

        exp_avg_p1 = weighted_add(exp_avg_p1_ptr, grad, self.beta1)
        exp_avg_p2 = weighted_add(exp_avg_p2_ptr, mtf.square(grad), self.beta2)

        return (self.learning_rate * exp_avg_p1 * mtf.rsqrt(exp_avg_p2 + self.epsilon),
                [mtf.assign(exp_avg_p1_ptr, exp_avg_p1), mtf.assign(exp_avg_p2_ptr, exp_avg_p2)])


class SGD(Optimizer):
    def apply_grad(self, grad, var):
        return grad * self.learning_rate, []


class NovoGrad(Optimizer):
    def apply_grad(self, grad: mtf.Tensor, var: mtf.Variable):
        """
        See base class.
        Applies Ranger optimizier to gradient/variable pairs.
        :param grad: Gradient for variable
        :param var: Variable to be updates
        :return: Update operations for variable and buffers
        """
        exp_avg_p1 = exp_avg_p1_ptr = self.variable(var, "exp_avg_p1", var.shape)
        exp_avg_p2 = exp_avg_p2_ptr = self.variable(var, "exp_avg_p1", [])

        exp_avg_p2 = weighted_add(exp_avg_p2, mtf.reduce_sum(mtf.square(grad)), self.beta2)
        update = self.beta1 * exp_avg_p1 + grad * mtf.rsqrt(exp_avg_p2 + self.epsilon)
        return (update * self.learning_rate,
                [mtf.assign(exp_avg_p1_ptr, self.beta1 * exp_avg_p1_ptr + grad * mtf.rsqrt(exp_avg_p2 + self.epsilon)),
                 mtf.assign(exp_avg_p2_ptr, exp_avg_p2)])


class FactorizedAdam(Optimizer):
    def apply_grad(self, grad, var):
        updates = []
        grad_factors = []

        for idx, dim in enumerate(var.shape.dims):
            dim = [dim]
            p1_ptr = self.variable(var, f"dim{idx}_p1", dim)
            p2_ptr = self.variable(var, f"dim{idx}_p2", dim)
            p1 = weighted_add(p1_ptr, mtf.reduce_mean(grad, output_shape=dim), self.beta1)
            p2 = weighted_add(p2_ptr, mtf.reduce_mean(mtf.square(grad), output_shape=dim), self.beta2)
            updates.extend([mtf.assign(p1_ptr, p1), mtf.assign(p2_ptr, p2)])
            grad_factors.append(p1 * mtf.rsqrt(p2 + self.epsilon))

        return mtf.add_n(grad_factors) * self.learning_rate / len(grad_factors), updates


class AdaHessian(Optimizer):
    def apply_grad(self, grad: mtf.Tensor, var: mtf.Variable):
        hess = grad
        uniform = mtf.cast(mtf.greater(mtf.random_uniform(var.mesh, var.shape), 0.5), var.dtype) * 2 - 1
        mtf.reduce_sum(uniform * grad)
        p1 = p1_ptr = self.variable(var, "p1", var.shape)
        p2 = p2_ptr = self.variable(var, "p2", var.shape)
        p1 = p1 + (grad - p1) * (1 - self.beta1)
        p2 = p2 + (mtf.square(hess) - p2) * (1 - self.beta2)
        return (self.learning_rate
                * p1
                * mtf.rsqrt(p2 / (1 - mtf.pow(self.beta2, self.global_step)) + self.epsilon)
                / (1 - mtf.pow(self.beta1, self.global_step)),
                [mtf.assign(p1_ptr, p1), mtf.assign(p2_ptr, p2)])


class SM3(Optimizer):
    def apply_grad(self, grad: mtf.Tensor, var: mtf.Variable):
        """
        See base class.
        Applies Ranger optimizier to gradient/variable pairs.
        :param grad: Gradient for variable
        :param var: Variable to be updates
        :return: Update operations for variable and buffers
        """
        rank = var.shape.ndims
        update = self.variable(var, "dim0", [var.shape.dims[0]])
        buffer = [update]

        for i in range(1, rank):
            buffer.append(self.variable(var, f"dim{i}", [var.shape.dims[i]]))
            update = mtf.minimum(update, buffer[-1])

        update += mtf.square(grad)

        return (grad * mtf.rsqrt(update + self.epsilon) * self.learning_rate,
                [mtf.assign(buf_ptr, mtf.reduce_max(update, output_shape=[dim]))
                 for buf_ptr, dim in zip(buffer, update.shape.dims)])


OPTIMIZERS = {'adam':            Adam,
              'novograd':        NovoGrad,
              'sm3':             SM3,
              'factorized_adam': FactorizedAdam,
              'sgd':             SGD
              }
