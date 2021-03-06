#!/usr/bin/python3

import gym
import ppaquette_gym_doom
from gym.wrappers import SkipWrapper
from ppaquette_gym_doom.wrappers.action_space import ToDiscrete
import os
import string
import argparse
import numpy as np
import tensorflow as tf
import tensorflow.contrib.layers as tflayers
from tqdm import trange
import collections

from matplotlib import pyplot as plt
plt.style.use("ggplot")

from wrappers import PreprocessImage


def create_if_need(path):
    if not os.path.exists(path):
        os.makedirs(path)


def plot_unimetric(history, metric, save_dir):
    plt.figure()
    plt.plot(history[metric])
    plt.title('model {}'.format(metric))
    plt.ylabel(metric)
    plt.xlabel('epoch')
    plt.savefig("{}/{}.png".format(save_dir, metric),
                format='png', dpi=300)


def save_stats(stats, save_dir="./"):
    for key in stats:
        plot_unimetric(stats, key, save_dir)


activations = {
    "sigmoid": tf.sigmoid,
    "tanh": tf.tanh,
    "relu": tf.nn.relu,
    "relu6": tf.nn.relu6,
    "elu": tf.nn.elu,
    "softplus": tf.nn.softplus
}


class DqnAgent(object):
    def __init__(self, state_shape, n_actions, network, gamma=0.99, special=None):
        self.special = special or {}
        self.state_shape = state_shape
        self.n_actions = n_actions
        self.buffer = collections.deque(maxlen=self.special.get("buffer_len", 10000))
        self.batch_size = self.special.get("batch_size", 32)

        self.current_states = tf.placeholder(shape=(None,) + state_shape, dtype=tf.float32)
        self.actions = tf.placeholder(shape=[None], dtype=tf.int32)
        self.rewards = tf.placeholder(shape=[None], dtype=tf.float32)
        self.next_states = tf.placeholder(shape=(None,) + state_shape, dtype=tf.float32)
        self.is_end = tf.placeholder(shape=[None], dtype=tf.bool)

        scope = self.special.get("scope", "network")

        self.predicted_qvalues = self.qnetwork(network, self.current_states, scope=scope)

        one_hot_actions = tf.one_hot(self.actions, n_actions)
        predicted_qvalues_for_actions = tf.reduce_sum(
            tf.multiply(self.predicted_qvalues, one_hot_actions),
            axis=-1)
        predicted_next_qvalues = self.qnetwork(network, self.next_states, scope=scope, reuse=True)

        target_qvalues_for_actions = self.rewards + \
            gamma * tf.reduce_max(predicted_next_qvalues, axis=-1)
        target_qvalues_for_actions = tf.where(
            self.is_end,
            tf.zeros_like(target_qvalues_for_actions),  # self.rewards
            target_qvalues_for_actions)

        self.loss = tf.reduce_mean(
            tf.square(target_qvalues_for_actions - predicted_qvalues_for_actions))

        self.update_step = tf.train.AdamOptimizer(self.special.get("lr", 1e-4)).minimize(
            self.loss, var_list=tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, scope=scope))

    def qnetwork(self, network, state, scope, reuse=False):
        hidden_state = network(state, scope=scope + "_hidden", reuse=reuse)
        qvalues = self._to_qvalues(hidden_state, scope=scope + "_qvalues", reuse=reuse)
        return qvalues

    def _to_qvalues(self, hidden_state, scope, reuse=False):
        with tf.variable_scope(scope) as scope:
            if reuse:
                scope.reuse_variables()
            qvalues = tflayers.fully_connected(
                hidden_state,
                num_outputs=self.n_actions,
                activation_fn=None)
            return qvalues

    def train_on_batch(self, sess, batch):
        state_batch = np.vstack(batch[:, 0]).reshape((-1, ) + self.state_shape)
        action_batch = np.vstack(batch[:, 1]).reshape(-1)
        reward_batch = np.vstack(batch[:, 2]).reshape(-1)
        next_state_batch = np.vstack(batch[:, 3]).reshape((-1, ) + self.state_shape)
        done_batch = np.vstack(batch[:, 4]).reshape(-1)
        
        loss, _ = sess.run(
            [self.loss, self.update_step],
            feed_dict={
                self.current_states: state_batch,
                self.actions: action_batch,
                self.rewards: reward_batch,
                self.next_states: next_state_batch,
                self.is_end: done_batch})

        return loss

    def observe(self, sess, state, action, reward, next_state, done):
        self.buffer.append(
            (state, action, reward, next_state, done))

        if len(self.buffer) > self.batch_size:
            batch_ids = np.random.choice(len(self.buffer), self.batch_size)
            batch = np.array([self.buffer[i] for i in batch_ids])
            return self.train_on_batch(sess, batch)

        return 0.0

    def _action(self, sess, state):
        qvalues = sess.run(
            self.predicted_qvalues,
            feed_dict={
                self.current_states: state})[0]
        action = np.argmax(qvalues)
        return action

    def _e_greedy_action(self, sess, state, epsilon=0.0):
        action = None
        if np.random.rand() < epsilon:
            action = np.random.choice(self.n_actions)
        else:
            action = self._action(sess, state)
        return action

    def action(self, sess, state, epsilon=0.0):
        return self._e_greedy_action(sess, state, epsilon)


def generate_session(sess, agent, env, epsilon=0.5, t_max=1000):
    """play env with approximate q-learning agent and train it at the same time"""

    total_reward = 0
    s = env.reset()
    total_loss = 0

    for t in range(t_max):
        a = agent.action(sess, np.array([s]), epsilon)

        new_s, r, done, info = env.step(a)

        curr_loss = agent.observe(sess, s, a, r, new_s, done)

        total_reward += r
        total_loss += curr_loss

        s = new_s
        if done:
            break

    return total_reward, total_loss / float(t+1), t


def q_learning(
        sess, agent, env, n_epochs, n_sessions=100, t_max=1000,
        initial_epsilon=0.25, final_epsilon=0.01):
    tr = trange(
        n_epochs,
        desc="mean reward = {:.3f}\tepsilon = {:.3f}\tloss = {:.3f}\tsteps = {:.3f}".format(
            0.0, 0.0, 0.0, 0.0),
        leave=True)

    epsilon = initial_epsilon
    n_epochs_decay = n_epochs * 0.8

    history = {
        "reward": np.zeros(n_epochs),
        "epsilon": np.zeros(n_epochs),
        "loss": np.zeros(n_epochs),
        "steps": np.zeros(n_epochs),
    }

    for i in tr:
        sessions = [generate_session(sess, agent, env, epsilon, t_max) for _ in range(n_sessions)]
        session_rewards, session_loss, session_steps = map(np.array, zip(*sessions))

        if i < n_epochs_decay:
            epsilon -= (initial_epsilon - final_epsilon) / float(n_epochs_decay)

        history["reward"][i] = np.mean(session_rewards)
        history["epsilon"][i] = epsilon
        history["loss"][i] = np.mean(session_loss)
        history["steps"][i] = np.mean(session_steps)

        tr.set_description(
            "mean reward = {:.3f}\tepsilon = {:.3f}\tloss = {:.3f}\tsteps = {:.3f}".format(
                history["reward"][i], history["epsilon"][i], history["loss"][i],
                history["steps"][i]))

    return history


def conv_network(states, scope, reuse=False, activation_fn=tf.nn.elu):
    with tf.variable_scope(scope or "network") as scope:
        if reuse:
            scope.reuse_variables()

        conv1 = tflayers.conv2d(
            states, 32, [5, 5], padding='SAME', activation_fn=activation_fn)
        conv1 = tflayers.conv2d(
            conv1, 32, [5, 5], padding='VALID', activation_fn=activation_fn)
        pool1 = tflayers.max_pool2d(conv1, [3, 3], padding='VALID')
        # pool1 = tflayers.dropout(pool1, keep_prob=keep_prob, is_training=is_training)

        conv2 = tflayers.conv2d(
            pool1, 32, [5, 5], padding='SAME', activation_fn=activation_fn)
        conv2 = tflayers.conv2d(
            conv2, 32, [5, 5], padding='VALID', activation_fn=activation_fn)
        pool2 = tflayers.max_pool2d(conv2, [3, 3], padding='VALID')
        # pool2 = tflayers.dropout(pool2, keep_prob=keep_prob, is_training=is_training)

        flat = tflayers.flatten(pool2)

        logits = tflayers.fully_connected(
            flat,
            512,
            activation_fn=activation_fn)
        return logits


def network_wrapper(activation_fn=tf.tanh):
    def wrapper(states, scope=None, reuse=False):
        return conv_network(states, scope, reuse, activation_fn=activation_fn)

    return wrapper


def _parse_args():
    parser = argparse.ArgumentParser(description='Doom Q-learning')
    parser.add_argument('--env',
                        type=str,
                        default='ppaquette/DoomBasic-v0',
                        help='The environment to use')
    parser.add_argument('--n_epochs',
                        type=int,
                        default=1000)
    parser.add_argument('--gamma',
                        type=float,
                        default=0.99,
                        help='Gamma discount factor')
    parser.add_argument('--plot_stats',
                        action='store_true',
                        default=False)
    parser.add_argument('--api_key',
                        type=str,
                        default=None)
    parser.add_argument('--activation',
                        type=str,
                        default="tanh")
    parser.add_argument('--batch_size',
                        type=int,
                        default=32)
    parser.add_argument('--buffer_len',
                        type=int,
                        default=10000)
    parser.add_argument('--initial_epsilon',
                        type=float,
                        default=0.99,
                        help='Gamma discount factor')
    parser.add_argument('--load',
                        action='store_true',
                        default=False)
    parser.add_argument('--gpu_option',
                        type=float,
                        default=0.4)
    parser.add_argument('--initial_lr',
                        type=float,
                        default=1e-4)

    args, _ = parser.parse_known_args()
    return args


def run(env, n_epochs, discount_factor,
        plot_stats=False, api_key=None,
        network=None, batch_size=32, buffer_len=10000, initial_epsilon=0.25,
        load=False, gpu_option=0.4, initial_lr=1e-4):
    env_name = env
    make_env = lambda: PreprocessImage(
        SkipWrapper(4)(ToDiscrete("minimal")(gym.make(env_name))),
        width=80, height=80, grayscale=True)
    env = make_env()

    n_actions = env.action_space.n
    state_shape = env.observation_space.shape
    special = {
        "batch_size": batch_size,
        "buffer_len": buffer_len,
        "lr": initial_lr
    }

    network = network or conv_network
    agent = DqnAgent(
        state_shape, n_actions, network,
        gamma=discount_factor,
        special=special)

    gpu_options = tf.GPUOptions(per_process_gpu_memory_fraction=gpu_option)

    with tf.Session(config=tf.ConfigProto(gpu_options=gpu_options)) as sess:
        saver = tf.train.Saver()
        model_dir = "./logs_" + env_name.replace(string.punctuation, "_")
        if not load:
            sess.run(tf.global_variables_initializer())
        else:
            saver.restore(sess, "{}/model.ckpt".format(model_dir))

        stats = q_learning(sess, agent, env, n_epochs, initial_epsilon=initial_epsilon)
        create_if_need(model_dir)
        saver.save(sess, "{}/model.ckpt".format(model_dir))

        if plot_stats:
            save_stats(stats)

        if api_key is not None:
            env = gym.wrappers.Monitor(env, "{}/monitor".format(model_dir), force=True)
            sessions = [generate_session(sess, agent, env, 0.0, int(1e10)) for _ in range(300)]
            env.close()
            gym.upload("{}/monitor".format(model_dir), api_key=api_key)


def main():
    args = _parse_args()
    network = network_wrapper(activations[args.activation])
    run(args.env, args.n_epochs, args.gamma,
        args.plot_stats, args.api_key,
        network, args.batch_size, args.buffer_len, args.initial_epsilon,
        args.load, args.gpu_option, args.initial_lr)


if __name__ == '__main__':
    main()
