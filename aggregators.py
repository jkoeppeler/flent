## -*- coding: utf-8 -*-
##
## aggregators.py
##
## Author:   Toke Høiland-Jørgensen (toke@toke.dk)
## Date:     16 oktober 2012
## Copyright (c) 2012, Toke Høiland-Jørgensen
##
## This program is free software: you can redistribute it and/or modify
## it under the terms of the GNU General Public License as published by
## the Free Software Foundation, either version 3 of the License, or
## (at your option) any later version.
##
## This program is distributed in the hope that it will be useful,
## but WITHOUT ANY WARRANTY; without even the implied warranty of
## MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
## GNU General Public License for more details.
##
## You should have received a copy of the GNU General Public License
## along with this program.  If not, see <http://www.gnu.org/licenses/>.

import math, pprint
from datetime import datetime

import runners, transformers

from util import classname

class Aggregator(object):
    """Basic aggregator. Runs all jobs and returns their result."""

    def __init__(self, config, logfile=None):
        self.iterations = int(config['iterations'])
        self.binary = config['cmd_binary']
        self.global_options = config['cmd_opts']
        self.default_runner = config.get('runner', 'default')
        self.instances = {}
        if logfile is None:
            self.logfile = None
        else:
            self.logfile = open(logfile, "a")

        self.postprocessors = []

    def add_instance(self, name, config):
        instance = {
            'options': self.global_options + " " + config.get('cmd_opts', ''),
            'delay': float(config.get('delay', 0)),
            'runner': getattr(runners, classname(config.get('runner', self.default_runner), 'Runner')),
            'binary': config.get('cmd_binary', self.binary),
            'config': config}

        # If an instance has the separate_opts set, do not combine the command
        # options with the global options.
        if config.get('separate_opts', False):
            instance['options'] = config.get('cmd_opts', '')

        if 'data_transform' in config:
            instance['transformers'] = []
            for t in [i.strip() for i in config['data_transform'].split(',')]:
                if hasattr(transformers, t):
                    instance['transformers'].append(getattr(transformers, t))

        duplicates = config.get('duplicates', None)
        if duplicates is not None:
            for i in xrange(int(duplicates)):
                self.instances["%s - %d" % (name, i+1)] = instance
        else:
            self.instances[name] = instance

    def aggregate(self):
        """Create a ProcessRunner thread for each instance and start them. Wait
        for the threads to exit, then collect the results."""

        if self.logfile:
            self.logfile.write(u"Start run at %s\n" % datetime.now())

        result = {}
        threads = {}
        for n,i in self.instances.items():
            threads[n] = i['runner'](n, i['binary'], i['options'], i['delay'], i['config'])
            threads[n].start()
        for n,t in threads.items():
            t.join()
            self._log(n,t)
            if t.result is None:
                continue
            elif callable(t.result):
                # If the result is callable, the runner is really a
                # post-processor (Avg etc), and should be run as such (by the
                # postprocess() method)
                self.postprocessors.append(t.result)
            else:
                result[n] = t.result
                if 'transformers' in self.instances[n]:
                    for tr in self.instances[n]['transformers']:
                        result[n] = tr(result[n])

        if self.logfile is not None:
            self.logfile.write("Raw aggregated data:\n")
            pprint.pprint(result, self.logfile)
        return result

    def postprocess(self, result):
        for p in self.postprocessors:
            result = p(result)
        if self.logfile is not None:
            self.logfile.write("Postprocessed data:\n")
            pprint.pprint(result, self.logfile)
        return result

    def _log(self, name, runner):
        if self.logfile is None:
            return
        self.logfile.write("Runner: %s - %s\n" % (name, runner.__class__.__name__))
        self.logfile.write("Command: %s\nReturncode: %d\n" % (runner.command, runner.returncode))
        self.logfile.write("Program stdout:\n")
        self.logfile.write("  " + "\n  ".join(runner.out.splitlines()) + "\n")
        self.logfile.write("Program stderr:\n")
        self.logfile.write("  " + "\n  ".join(runner.err.splitlines()) + "\n")

class IterationAggregator(Aggregator):
    """Iteration aggregator. Runs the jobs multiple times and aggregates the
    results. Assumes each job outputs one value."""

    def aggregate(self):
        results = []
        for i in range(self.iterations):
            results.append(((i+1), Aggregator.aggregate(self)))
        return results

class TimeseriesAggregator(Aggregator):
    """Time series aggregator. Runs the jobs (which are all assumed to output a
    series of timed entries) and combines the times onto a single timeline,
    aligning values to the same time steps (interpolating values as necessary).
    Assumes each job outputs a list of pairs (time, value) where the times and
    values are floating point values."""

    def __init__(self, config, *args, **kwargs):
        self.step = float(config['step'])
        self.max_distance = float(config['max_distance'])
        Aggregator.__init__(self, config, *args, **kwargs)

    def aggregate(self):
        measurements = Aggregator.aggregate(self)
        results = []

        # We start steps at the minimum time value, and do as many steps as are
        # necessary to get past the maximum time value with the selected step
        # size
        first_times = [i[0][0] for i in measurements.values() if i[0]]
        last_times = [i[-1][0] for i in measurements.values() if i[-1]]
        if not (first_times or last_times):
            raise RuntimeError(u"No data to aggregate")
        t_0 = min(first_times)
        t_max = max(last_times)
        steps = int(math.ceil((t_max-t_0)/self.step))

        time_labels = []

        for s in range(steps):
            time_labels.append(self.step*s)
            t = t_0 + self.step*s

            # for each step we need to find the interpolated measurement value
            # at time t by interpolating between the nearest measurements before
            # and after t
            result = {}
            # n is the name of this measurement (from the config), r is the list
            # of measurement pairs (time,value)
            for n,r in measurements.items():
                t_prev = v_prev = None
                t_next = v_next = None
                for i in range(len(r)):
                    if r[i][0] > t:
                        if i > 0:
                            t_prev,v_prev = r[i-1]
                        t_next,v_next = r[i]
                        break
                if t_next is None:
                    t_next,v_next = r[-1]
                if abs(t-t_next) <= self.max_distance:
                    if t_prev is None:
                        # The first data point for this measurement is after the
                        # current t. Don't interpolate, just use the value.
                        result[n] = v_next
                    else:
                        # We found the previous and next values; interpolate between
                        # them. We assume that the rate of change dv/dt is constant
                        # in the interval, and so can be calculated as
                        # (v_next-v_prev)/(t_next-t_prev). Then the value v_t at t
                        # can be calculated as v_t=v_prev + dv/dt*(t-t_prev)

                        dv_dt = (v_next-v_prev)/(t_next-t_prev)
                        result[n] = v_prev + dv_dt*(t-t_prev)
                else:
                    # Interpolation distance is too long; don't use the value.
                    result[n] = None

            results.append(result)

        return zip(time_labels, results)