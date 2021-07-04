"""
Stores custom optimizer classes as well as a custom optimizer creation utility as a handy wrapper
b"""

import typing

import mesh_tensorflow as mtf
import numpy as np
import tensorflow as tf2
from tensorflow.python.ops.init_ops import Initializer

from src.model.revnet import RevGradOp
from .dataclass import ModelParameter
from .mtf_wrapper import (add_n, cast, constant_float, constant_scalar, einsum, equal, greater, greater_equal, minimum,
                          mod, reduce_max, reduce_mean, reduce_sum, rsqrt, sqrt, square, assign, assign_sub,
                          one_hot as mtf_one_hot, logical_and, add, multiply, identity, import_fully_replicated,
                          reshape, scoped, reciprocal, constant, negative)
from .utils_mtf import SHAPE, feature_dims_used, to_fp32, weighted_add, get_variable

tf = tf2.compat.v1
zeros = tf.zeros_initializer()


def import_float(imported):
    return tf.constant(imported, dtype=tf.float32, shape=[])


def get_var(params: ModelParameter, name: str, shape: SHAPE, initializer: Initializer = zeros):
    return get_variable(params, name, shape, initializer, False, params.optimizer_dtype)


def variable(params: ModelParameter, base: mtf.Variable, name: str, shape: SHAPE):
    return get_variable(params, f"{base.name}/{params.optimizer}/{name}", shape, zeros, False, params.optimizer_dtype)


def update(op: mtf.Operation, grad_outputs: typing.List[mtf.Tensor], downstream: typing.Set[mtf.Operation],
           tensor_to_gradient: dict, tensor_to_var: dict, params: ModelParameter, loss_idx: int, update_ops: list,
           debug_gradients_dict: dict, loss_list: list, first_grad: dict, loss_1__loss_1: typing.Optional[mtf.Tensor],
           loss_1__loss_2: typing.Optional[mtf.Tensor], loss_2__loss_2: typing.Optional[mtf.Tensor], mstep: mtf.Tensor,
           step: mtf.Tensor, dtype: mtf.VariableDType, beta1: mtf.Tensor, beta2: mtf.Tensor, epsilon: mtf.Tensor,
           learning_rate: mtf.Tensor):
    if isinstance(op, RevGradOp):
        itr = op.gradient(grad_outputs, params=op.inputs)
    else:
        itr = zip(op.inputs, op.gradient(grad_outputs))
    for inp, grad in itr:
        if inp not in downstream or grad is None:
            continue

        if inp in tensor_to_gradient:
            grad_list = tensor_to_gradient[inp]
            grad_list[1] += 1
            grad_list[2] = add(loss_list[2], grad)
        else:
            tensor_to_gradient[inp] = grad_list = [0, 1, grad]

        if len(inp.operation.outputs) != grad_list[1] or inp not in tensor_to_var:
            continue

        grad: mtf.Tensor = grad_list[2]
        var: mtf.Variable = tensor_to_var[inp]

        if params.debug_gradients:
            flat_shape = mtf.Shape([mtf.Dimension('flat_dim', var.size)])
            flat_grad = variable(params, var, f"loss_{loss_idx}", flat_shape)
            update_ops.append(assign(flat_grad, reshape(grad, new_shape=flat_shape)))
            debug_gradients_dict[f"loss_{loss_idx}/{var.name}"] = flat_grad

        if len(loss_list) > 1:
            if params.multi_loss_strategy == "pcgrad":
                if 'body' in op.name:

                    if loss_idx < len(loss_list) - 1:
                        first_grad[op.name] = grad
                        continue

                    else:

                        all_grads = [grad, first_grad[op.name]]
                        g_square = [1e-6 + einsum([g, g], output_shape=[]) for g in all_grads[1:]]

                        for i in range(len(all_grads)):
                            grad = all_grads.pop(0)
                            for g, sq in zip(all_grads, g_square):
                                grad -= g * (minimum(einsum([grad, g], output_shape=[]), 0) / sq)

                            all_grads.append(grad)
                            g_square.append(einsum([g, g], output_shape=[]))

                        grad = add_n(all_grads)

                elif params.multi_loss_strategy == "mgda":
                    if 'body' in op.name:
                        if loss_idx < 2:
                            if loss_idx == 0:
                                first_grad[op.name] = grad
                                continue

                            else:

                                loss_1__loss_1 += einsum([first_grad[op.name], first_grad[op.name]],
                                                         [params.head_dim])
                                loss_1__loss_2 += einsum([first_grad[op.name], grad], [params.head_dim])
                                loss_2__loss_2 += einsum([grad, grad], [params.head_dim])

                                del first_grad[op.name]
                                continue

                    elif loss_idx == 2:  # not in body and optimize body params.
                        continue

        if params.grad_accumulation > 1:
            grad_buffer = variable(params, var, "grad_accumulation", var.shape)
            next_grad = grad + identity(grad_buffer)
            update_ops.append(assign(grad_buffer, next_grad * mstep))
            grad = einsum([next_grad, step, constant(params, 1 / params.grad_accumulation)],
                          output_shape=next_grad.shape)

        features_used = feature_dims_used(params, var)
        if features_used and var.shape.dims.index(params.key_dim) == var.shape.ndims - 1:
            fan_in = var.shape.dims[:-2]
        elif features_used:
            fan_in = var.shape.dims[:2]
        else:
            fan_in = var.shape.dims[:1]
        if params.gradient_clip > 0 and params.adaptive_gradient_clipping:
            grd_norm = sqrt(add(einsum([grad, grad], reduced_dims=fan_in), 1e-5))
            wgt_norm = sqrt(add(einsum([var.value, var.value], reduced_dims=fan_in), 1e-3))
            grad = weighted_add(einsum([grd_norm, reciprocal(wgt_norm), constant(params, params.gradient_clip), grad],
                                       output_shape=grad.shape),
                                grad, cast(greater(wgt_norm / grd_norm, params.gradient_clip), dtype))
        elif params.gradient_clip > 0:
            grad = einsum([minimum(rsqrt(add(einsum([grad, grad], []), 1e-6)), 1 / params.gradient_clip),
                           grad, constant_scalar(params, params.gradient_clip)], grad.shape)
        if var.shape.ndims <= 1 or params.optimizer == 'adam':
            exp_avg_p2_ptr = variable(params, var, 'exp_avg_p2', var.shape)
            exp_avg_p2 = weighted_add(exp_avg_p2_ptr, square(grad), beta2)
            update_ops.append(assign(exp_avg_p2_ptr, exp_avg_p2))
            if params.opt_beta1:
                exp_avg_p1_ptr = variable(params, var, 'exp_avg_p1', var.shape)
                grad = weighted_add(exp_avg_p1_ptr, grad, beta1)
                update_ops.append(assign(exp_avg_p1_ptr, grad))
            weight_update = multiply(grad, rsqrt(add(exp_avg_p2, epsilon)))

        elif params.optimizer == 'sgd':
            weight_update = grad

        elif params.optimizer == 'novograd':
            exp_avg_p1 = exp_avg_p1_ptr = variable(params, var, "exp_avg_p1", var.shape)
            exp_avg_p2 = exp_avg_p2_ptr = variable(params, var, "exp_avg_p2", [])

            exp_avg_p2 = weighted_add(exp_avg_p2, reduce_sum(square(grad)), beta2)
            weight_update = add(multiply(beta1, exp_avg_p1), multiply(grad, rsqrt(exp_avg_p2 + epsilon)))
            update_ops.extend([assign(exp_avg_p1_ptr, add(multiply(beta1, exp_avg_p1_ptr),
                                                          multiply(grad, rsqrt(exp_avg_p2 + epsilon)))),
                               assign(exp_avg_p2_ptr, exp_avg_p2)])

        elif params.optimizer == 'sm3':
            update = variable(params, var, "dim0", [var.shape.dims[0]])
            buffer = [update]

            for i in range(1, var.shape.ndims):
                buffer.append(variable(params, var, f"dim{i}", [var.shape.dims[i]]))
                update = minimum(update, buffer[-1])

            update += square(grad)

            weight_update = grad * rsqrt(update + epsilon)
            update_ops.extend([assign(buf_ptr, reduce_max(update, output_shape=[dim]))
                               for buf_ptr, dim in zip(buffer, update.shape.dims)])

        weight_update = multiply(weight_update, learning_rate)

        large_tensor = features_used and len(var.shape.dims) > len(params.feature_dims)
        large_tensor |= not features_used and len(var.shape.dims) >= 2  # not norm or rezero + scalable catch-all
        large_tensor &= var.shape.size > 1  # not rezero
        large_tensor &= "embed" not in var.name  # not input/output embedding, position embedding, attention map bias
        large_tensor &= "input" not in var.name or "lang_in" in var.name or "vid_in" in var.name  # not input
        large_tensor &= "output" not in var.name or "lang_out" in var.name or "vid_out" in var.name  # not output

        if 'rezero' in var.name:
            weight_update = multiply(weight_update, params.rezero_lr_multiplier)
        if large_tensor and params.weight_decay > 0:
            weight_update = add(var.value,
                                einsum([constant(params, params.weight_decay), var.value, learning_rate], var.shape))
        if large_tensor and params.weight_centralisation:
            weight_update = add(weight_update, reduce_mean(var.value))
        if params.grad_accumulation > 1:
            weight_update = multiply(weight_update, step)
        if large_tensor and params.weight_standardisation:
            val: mtf.Tensor = add(var.value, negative(weight_update))
            std = rsqrt(add(1e-6, einsum([val, val, constant(params, val.size ** -0.5)], output_shape=[])))
            shape = [d.size for d in var.shape.dims]
            fan_in_size = np.prod([d.size for d in fan_in])
            size = np.prod(shape)
            val = einsum([constant(params, ((fan_in_size - 2) / size / params.n_blocks) ** 0.5),
                          val, std], output_shape=var.shape)
            update_ops.append(assign(var, val))
        else:
            update_ops.append(assign_sub(var, weight_update))


def get_optimizer(loss_list: typing.List[mtf.Tensor], params: ModelParameter, manual_step: tf.Tensor,
                  ) -> typing.Tuple[typing.Tuple[mtf.Tensor, typing.List[mtf.Assign], typing.List[mtf.Tensor]],
                                    tf.Tensor, typing.Dict]:
    """
    Creates optimizing and update/training operations.
    :param loss_list: Final scalar loss of the model
    :param params: ModelParameter instance
    :param manual_step: manually incremented global_step variable to account for grad accumulation
    :return: scalar learning rate, update operations, gradients
    """

    global_step = tf.train.get_or_create_global_step()
    dtype = params.variable_dtype.activation_dtype
    tf_learning_rate = tf.constant(value=params.learning_rate, shape=[], dtype=tf.float32)
    global_steps_float = tf.cast(global_step, tf.float32)
    global_step_mtf = import_fully_replicated(params, global_step, [], "mtf_learning_rate")
    update_ops = []

    def import_mtf(imported, name):
        return import_fully_replicated(params, tf.cast(imported, dtype), [], name)

    if params.warmup_steps > 0:
        warmup_steps_float = import_float(params.warmup_steps)
        is_warmup = tf.cast(global_steps_float < warmup_steps_float, tf.float32)
        tf_learning_rate = tf_learning_rate * weighted_add(global_steps_float / warmup_steps_float, 1, is_warmup)

    if params.learning_rate_decay_multi != 0 and params.learning_rate_decay_multi != 1:
        start_step = import_float(params.learning_rate_decay_start_step * 1.)
        tf_learning_rate = tf.maximum(tf_learning_rate *
                                      import_float(params.learning_rate_decay_multi * 1.) **
                                      (tf.maximum(global_steps_float - start_step, import_float(0.))),
                                      import_float(params.learning_rate_decay_min * 1.))

    if params.reduce_lr_on_plateau_timespan:
        base = "reduce_lr_on_plateau/"
        loss_sum = add_n(loss_list)
        window_dim = mtf.Dimension("loss_window", params.reduce_lr_on_plateau_timespan)

        divisor_ptr = get_var(params, f"{base}lr_divisor", [], tf.ones_initializer())
        loss_window_ptr = get_var(params, f"{base}loss_window", [window_dim])
        loss_ema_ptr = get_var(params, f"{base}loss_ema", [])
        last_reduce = get_var(params, f"{base}last_reduce", [])

        one_hot = mtf_one_hot(mod(global_step_mtf, params.reduce_lr_on_plateau_timespan), window_dim)
        sub = (loss_sum - loss_window_ptr) * one_hot
        loss_window = loss_window_ptr - sub
        window_mean = reduce_mean(loss_window, output_shape=[])
        loss_ema = loss_ema_ptr * (2 / params.reduce_lr_on_plateau_timespan)
        loss_ema += loss_sum * (1 - 2 / params.reduce_lr_on_plateau_timespan)
        reduce = cast(logical_and(greater(global_step_mtf, last_reduce + params.reduce_lr_on_plateau_timespan),
                                  greater(window_mean, loss_ema)),
                      params.variable_dtype.activation_dtype)
        reduce = reduce * (params.reduce_lr_on_plateau_reduction - 1) + 1
        divisor = divisor_ptr * reduce
        tf_learning_rate /= divisor

        update_ops.append(assign(divisor_ptr, divisor))
        update_ops.append(assign_sub(loss_window_ptr, sub))
        update_ops.append(assign(loss_ema_ptr, loss_ema))
        update_ops.append(assign(last_reduce, weighted_add(last_reduce, global_step_mtf, reduce)))

    learning_rate = import_mtf(tf_learning_rate, "learning_rate")
    step = cast(equal(mod(tf.cast(manual_step + 1, dtype),
                          import_mtf(params.grad_accumulation * 1., "grad_accum")),
                      import_mtf(0., "zero")), dtype)
    mstep = 1 - step
    beta1 = 1 - step * import_mtf(1 - params.opt_beta1, "beta1") if params.opt_beta1 else None
    beta2 = 1 - step * import_mtf(1 - params.opt_beta2, "beta2")
    epsilon = params.opt_epsilon

    debug_gradients_dict = {}
    first_grad = {}
    loss_1__loss_1 = loss_1__loss_2 = loss_2__loss_2 = 0
    if params.multi_loss_strategy == "mgda":
        loss_1__loss_1 = constant_float(params, 0, shape=[params.head_dim])
        loss_1__loss_2 = constant_float(params, 0, shape=[params.head_dim])
        loss_2__loss_2 = constant_float(params, 0, shape=[params.head_dim])

    for loss_idx, loss in enumerate(loss_list):

        if params.multi_loss_strategy == "mgda" and loss_idx == 2:
            v1v1 = reduce_sum(loss_1__loss_1, output_shape=[])
            v1v2 = reduce_sum(loss_1__loss_2, output_shape=[])
            v2v2 = reduce_sum(loss_2__loss_2, output_shape=[])
            min_gamma = 0.001
            gamma = constant_float(params, value=(1 - min_gamma), shape=[]) * to_fp32(greater_equal(v1v2, v1v1))
            gamma += constant_float(params, value=min_gamma, shape=[]) * \
                     to_fp32(greater_equal(v1v2, v2v2)) * to_fp32(equal(gamma, 0))
            gamma += (-1.0 * ((v1v2 - v2v2) / (v1v1 + v2v2 - 2 * v1v2))) * to_fp32(equal(gamma, 0))

            loss = add(multiply(loss_list[0], gamma),
                       multiply(loss_list[1], (1 - gamma)))

        operations = loss.graph.operations
        xs = [x.outputs[0] for x in params.mesh.graph.trainable_variables]
        tensor_to_var = dict(zip(xs, params.mesh.graph.trainable_variables))
        loss_grad = constant_scalar(params, 1.0)
        downstream = set(xs)

        for op in operations:
            if op.has_gradient and (set(op.inputs) & downstream):
                downstream |= set(op.outputs)

        tensor_to_gradient: typing.Dict[mtf.Tensor, typing.List[int, int, mtf.Tensor]] = {loss: [0, 0, loss_grad]}

        with tf.variable_scope(loss.graph.captured_variable_scope):
            for op in operations[::-1]:
                grad_outputs = []
                for out in op.outputs:
                    if out not in tensor_to_gradient:
                        grad_outputs.append(None)
                        continue

                    grad_list: typing.Tuple[int, int, mtf.Tensor] = tensor_to_gradient[out]
                    grad_outputs.append(grad_list[2])
                    grad_list[0] += 1

                    if grad_list[0] == len(grad_list[2].operation.inputs):
                        del tensor_to_gradient[out]

                if not op.has_gradient or not any(grad_outputs) or not (set(op.inputs) & downstream):
                    continue
                scoped("update", update, op, grad_outputs, downstream, tensor_to_gradient, tensor_to_var, params,
                       loss_idx, update_ops, debug_gradients_dict, loss_list, first_grad, loss_1__loss_1,
                       loss_1__loss_2, loss_2__loss_2, mstep, step, dtype, beta1, beta2, epsilon, learning_rate)
    return params.mesh.graph.trainable_variables[0].graph.combine_assignments(update_ops), \
           tf_learning_rate, debug_gradients_dict
