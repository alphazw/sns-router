# -*- coding: utf-8 -*-

import sys
sys.path.append('../bottle')
sys.path.append('../snsapi')
import snsapi
from snsapi.snspocket import SNSPocket
from snsapi.platform import SQLite
from snsapi import utils as snsapi_utils
from snsapi import snstype
# On my server, pickle uses 24s to load 
# cPickle uses 6s to load. 
# very significant
#from snsapi.utils import Serialize
import cPickle as Serialize
from snsapi.snsbase import SNSBase
from snsapi.snslog import SNSLog as logger

from feature import Feature
from evaluation import evaluate_kendall

import base64
import hashlib
import sqlite3
import random
import numpy

class Learner(object):
    """
    Base class for a learner
    
    One can implement different objective and gradient here
    """
    def __init__(self):
        super(Learner, self).__init__()

    def objective(self, X, w, order):
        pass

    def gradient(self, X, w, order):
        pass

class LearnerSigmoid(Learner):
    def __init__(self):
        super(LearnerSigmoid, self).__init__()
        
    def _S(self, t):
        return 1.0 / (1.0 + numpy.exp(-t))
    
    def _dS(self, t):
        return self._S(t) * (1 - self._S(t))

    def _Gij(self, xi, xj, w):
        '''
        Compute per pair gradient

        xi, xj: data points
        w: current weights

        xi, xj, and w are all lists
        '''
        inner = 0.0
        for k in range(len(w)):
            inner += (xi[k] - xj[k]) * w[k]
        part2 = self._dS(inner)
        ret = []
        for k in range(len(w)):
            ret.append(-(xi[k] - xj[k]) * part2)
        return ret

    def _G(self, X, w, order):
        '''
        Compute full gradient

        X: A dict of data points. 
           * values: One data point is a list.
           * keys: msg_id from database. 
        w: current weights
        order: derived order (edges on priority graph)
        '''
        #N = 1.0 * len(order)
        G = [0] * len(w)
        for (i,j) in order:
            Gij = self._Gij(X[i], X[j], w)
            for k in range(len(w)):
                #G[k] += (Gij[k] / N)
                G[k] += Gij[k] 
        return G

    def objective(self, X, w, order):
        #N = 1.0 * len(order)
        o = 0.0
        for (i,j) in order:
            inner = 0.0
            for k in range(len(w)):
                inner += (X[i][k] - X[j][k]) * w[k]
            #o += (1 - self._S(inner)) / N
            o += (1 - self._S(inner)) 
        return o

    def gradient(self, X, w, order):
        return self._G(X, w, order)
        
class LearnerSquareSigmoid(Learner):
    """docstring for LearnerSquareSigmoid"""
    def __init__(self):
        super(LearnerSquareSigmoid, self).__init__()
        
    def _S(self, t):
        return 1.0 / (1.0 + numpy.exp(-t))
    
    def _dS(self, t):
        return self._S(t) * (1 - self._S(t))

    def _Gij(self, xi, xj, w):
        '''
        Compute per pair gradient

        xi, xj: data points
        w: current weights

        xi, xj, and w are all lists
        '''
        inner = 0.0
        for k in range(len(w)):
            inner += (xi[k] - xj[k]) * w[k]
        part1 = 2.0 * (self._S(inner) - 1)
        part2 = self._dS(inner)
        ret = []
        for k in range(len(w)):
            ret.append((xi[k] - xj[k]) * part1 * part2)
        return ret

    def _G(self, X, w, order):
        '''
        Compute full gradient

        X: A dict of data points. 
           * values: One data point is a list.
           * keys: msg_id from database. 
        w: current weights
        order: derived order (edges on priority graph)
        '''
        #N = 1.0 * len(order)
        G = [0] * len(w)
        for (i,j) in order:
            Gij = self._Gij(X[i], X[j], w)
            for k in range(len(w)):
                #G[k] += (Gij[k] / N)
                G[k] += Gij[k] 
        return G

    def objective(self, X, w, order):
        #N = 1.0 * len(order)
        o = 0.0
        for (i,j) in order:
            inner = 0.0
            for k in range(len(w)):
                inner += (X[i][k] - X[j][k]) * w[k]
            #o += (self._S(inner) - 1) ** 2 / N
            o += (self._S(inner) - 1) ** 2 
        return o

    def gradient(self, X, w, order):
        return self._G(X, w, order)

class AutoWeight(object):
    """docstring for AutoWeight"""

    def __init__(self, samples, order, init_weight, learner):
        super(AutoWeight, self).__init__()
        self.feature_name = init_weight.keys()
        self.w = self.initw(init_weight)
        self.X = self.msg2X(samples)
        self.samples = samples
        self.order = order
        self.learner = learner

    def _weight_feature(self, msg):
        Feature.extract(msg)
        score = 0.0
        for i in range(len(self.feature_name)):
            f = self.feature_name[i]
            w = self.w[i]
            if f in msg.feature:
                score += msg.feature[f] * w
        return score

    def normalize(self, w):
        t = 0.0
        for i in w:
            t += i * i
        t = numpy.sqrt(t)
        if t < 1e-5:
            t = 1.0
        return [i / t for i in w]

    def normalize_sum(self, w):
        t = 0.0
        for i in w:
            t += i
        t = numpy.sqrt(t)
        if numpy.abs(t) < 1e-5:
            t = 1.0
        return [i / t for i in w]

    def initw(self, init_weight):
        w = []
        for name in self.feature_name:
            w.append(init_weight[name])
        return self.normalize(w)

    def msg2X(self, samples):
        '''
        Convert messages to data matrix format. 

        X: A dict. See explanation of _G()
        '''
        X = {}
        for m in samples.values():
            Feature.extract(m)
            x = []
            for name in self.feature_name:
                x.append(m.feature[name])
            X[m.msg_id] = x
        return X

    def gd(self):
        a = 1e-2
        g = self.learner.gradient(self.X, self.w, self.order)
        g = self.normalize(g)
        print "Gradient: %s" % g
        new_w = []
        for i in range(len(self.w)):
            new_w.append(self.w[i] - a * g[i])
        #self.w = self.normalize(new_w)
        self.w = self.normalize_sum(new_w)
        print "New objective %.3f" % self.learner.objective(self.X, self.w, self.order)
        print "New weights: %s" % self.w

    def train(self):
        print "---- init ----"
        print "Weights: %s" % self.w
        print "Kendall's coefficient: %.3f" % self.evaluate()
        for i in range(5):
            print "Round %d" % i
            self.gd()
            print "Kendall's coefficient: %.3f" % self.evaluate()

    def evaluate(self):
        ranked = sorted(self.samples.values(), key = lambda m: self._weight_feature(m), reverse = True)
        ret = evaluate_kendall([m.msg_id for m in ranked], order)
        return ret

def init_weight_kendall(feature_name, samples, order):
    iw = {}
    for f in feature_name:
        print "Feature: %s" % f
        k = evaluate_kendall(sorted(samples.keys(), key = lambda m: samples[m].feature[f], reverse = True), order)
        iw[f] = k
    print "Init weight by Kendall: %s" % iw
    return iw


if __name__ == '__main__':
    import time
    begin = time.time()
    data = Serialize.loads(open('samples.pickle').read())
    samples = data['samples']
    order = data['order']
    end = time.time()
    print "Load finish. Time elapsed: %.3f" % (end - begin)

    aw = AutoWeight(samples, order, {
        "contain_link": 1, 
        "test": 1, 
        "text_len": 1, 
        "text_orig_len": 1, 
        "topic_interesting": 1, 
        "topic_news": 1, 
        "topic_nonsense": 1, 
        "topic_tech": 1
    }, LearnerSigmoid())
    
    aw.w = aw.initw(init_weight_kendall(aw.feature_name, aw.samples, aw.order))

    #aw = AutoWeight(samples, order, \
    #        init_weight_kendall(\
    #        ["contain_link", "test", "text_len", "text_orig_len", \
    #        "topic_interesting", "topic_news", "topic_nonsense", "topic_tech"], \
    #        samples, order)\
    #        , LearnerSigmoid())

    #aw = AutoWeight(samples, order, {
    #    "contain_link": 1.00000, 
    #    "test": 1.00000, 
    #    "text_len": 0, 
    #    "text_orig_len": 0.01, 
    #    "topic_interesting": 30, 
    #    "topic_news": 30, 
    #    "topic_nonsense": -100, 
    #    "topic_tech": 500
    #}, LearnerSigmoid())

    #aw = AutoWeight(samples, order, {
    #    "contain_link": 1.00000, 
    #    "test": 1.00000, 
    #    "text_len": 0, 
    #    "text_orig_len": 0.01, 
    #    "topic_interesting": 30, 
    #    "topic_news": 30, 
    #    "topic_nonsense": -100, 
    #    "topic_tech": 500
    #}, LearnerSquareSigmoid())

    #aw.train()

    #ranked = sorted(samples.values(), key = lambda m: random.random())
    #ranked = sorted(samples.values(), key = lambda m: m.parsed.time)
    #ranked = sorted(samples.values(), key = lambda m: aw._weight_feature(m), reverse = True)
    #ret = evaluate_kendall([m.msg_id for m in ranked], order)
    #print ret