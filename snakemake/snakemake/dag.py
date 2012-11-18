import textwrap, sys
from collections import defaultdict
from itertools import chain, combinations, filterfalse, product
from functools import partial, lru_cache
from operator import itemgetter

from snakemake.io import IOFile
from snakemake.jobs import Job, Reason
from snakemake.exceptions import RuleException, MissingInputException, MissingRuleException, AmbiguousRuleException, CyclicGraphException, MissingOutputException
from snakemake.logging import logger

class DAG:
	def __init__(self, 
	             workflow,
	             targetfiles = None, 
	             targetrules = None,
	             forceall = False,
	             forcetargets = False, 
	             forcerules = None,
	             priorityfiles = None,
	             priorityrules = None,
	             ignore_ambiguity = False):
		self.dependencies = defaultdict(partial(defaultdict, set))
		self.depending = defaultdict(partial(defaultdict, set))
		self._needrun = set()
		self._reason = defaultdict(Reason)
		self._finished = set()
		self._dynamic = set()
		self._len = 0
		self.workflow = workflow
		self.rules = workflow.rules
		self.ignore_ambiguity = ignore_ambiguity
		self.targetfiles = targetfiles
		self.targetrules = targetrules
		self.priorityfiles = priorityfiles
		self.priorityrules = priorityrules
		self.targetjobs = set()
		self.prioritytargetjobs = set()
				
		self.forcerules = set()
		self.forcefiles = set()
		if forceall:
			self.forcerules.update(self.rules)
		elif forcerules:
			self.forcerules.update(forcerules)
		if forcetargets:
			self.forcerules.update(targetrules)
			self.forcefiles.update(targetfiles)

	def init(self):
		
		#import pdb; pdb.set_trace()
		for job in map(self.rule2job, self.targetrules):
			job = self.update([job])
			self.targetjobs.add(job)
		
		exceptions = defaultdict(list)
		for file in self.targetfiles:
			try:
				job = self.update(self.file2jobs(file), file=file)
				self.targetjobs.add(job)
			except MissingRuleException as ex:
				exceptions[file].append(ex)
		
		if exceptions:
			raise RuleException(include=chain(*exceptions.values()))
		self.update_needrun()
		
		for job in filter(lambda job: job.dynamic_output and not self.needrun(job), self.jobs):
			self.update_dynamic(job)
		self.postprocess()

	@property
	def jobs(self):
		for job in self.bfs(self.dependencies, *self.targetjobs):
			yield job
	
	@property
	def needrun_jobs(self):
		for job in filter(self.needrun, self.bfs(self.dependencies, *self.targetjobs, stop=self.noneedrun_finished)):
			yield job
			
	@property
	def ready_jobs(self):
		for job in filter(self.ready, self.needrun_jobs):
			yield job
			
	def ready(self, job):
		return all(map(lambda job: self.finished(job) or not self.needrun(job), self.dependencies[job]))
	
	def needrun(self, job):
		return job in self._needrun
	
	def noneedrun_finished(self, job):
		return not self.needrun(job) or self.finished(job)
	
	def reason(self, job):
		return self._reason[job]
	
	def finished(self, job):
		return job in self._finished
	
	def dynamic(self, job):
		return job in self._dynamic
		
	def requested_files(self, job):
		return set(*self.depending[job].values())
	
	def missing_temp(self, job):
		for job_, files in self.depending[job].items():
			if self.needrun(job_) and any(not f.exists for f in files):
				return True
		return False
		
	def check_output(self, job):
		for f in job.expanded_output:
			if not f.exists:
				raise MissingOutputException("Output file {} not produced by rule {}.".format(f, job.rule.name), lineno = job.rule.lineno, snakefile = job.rule.snakefile)
	
	def handle_protected(self, job):
		for f in job.expanded_output:
			if f in job.protected_output:
				logger.warning("Write protecting output file {}".format(f))
				f.protect()
	
	def handle_temp(self, job):
		needed = lambda job_, f: any(f in files for j, files in self.depending[job_].items() if not self.finished(j) and j != job)
		for job_ , files in self.dependencies[job].items():
			for f in job_.temp_output & files:
				if not needed(job_, f):
					logger.warning("Removing temporary output file {}".format(f))
					f.remove()
	
	def update(self, jobs, file=None, visited = None, skip_until_dynamic = False):
		if visited is None:
			visited = set()
		producer = None
		exceptions = list()
		jobs = sorted(jobs, reverse=not self.ignore_ambiguity)
		cycles = list()
		for i, job in enumerate(jobs):
			if job in visited:
				cycles.append(job)
				continue
			try:
				self.update_(job, visited=set(visited), skip_until_dynamic=skip_until_dynamic)
				if i > 0:
					if job < jobs[i-1] or self.ignore_ambiguity:
						break
					elif producer is not None:
						raise AmbiguousRuleException(file, job.rule, jobs[i-1].rule)
				producer = job
			except (MissingInputException, CyclicGraphException) as ex:
				exceptions.append(ex)
			except RuntimeError as ex:
				if isinstance(ex, RuntimeError) and str(ex).startswith("maximum recursion depth exceeded"):
					ex = RuleException("Maximum recursion depth exceeded. Maybe you have a cyclic dependency due to infinitely filled wildcards?\nProblematic input file:\n{}".format(file), rule = job.rule)
				raise ex
		if producer is None:
			if cycles:
				raise CyclicGraphException(cycles[0].rule, file, rule=job.rule)
			if exceptions:
				raise exceptions[0]
		return producer
	
	def update_(self, job, visited = None, skip_until_dynamic = False):
		if job in self.dependencies:
			return
		if visited is None:
			visited = set()
		visited.add(job)
		dependencies = self.dependencies[job]
		potential_dependencies = self.collect_potential_dependencies(job).items()

		skip_until_dynamic = skip_until_dynamic and not job.dynamic_output
		
		producer = dict()
		exceptions = dict()
		for file, jobs in potential_dependencies:
			# TODO check for pumping up wildcards...
			try:
				producer[file] = self.update(jobs, file=file, visited=visited,
				                             skip_until_dynamic=skip_until_dynamic or file in job.dynamic_input)
			except (MissingInputException, CyclicGraphException) as ex:
				exceptions[file] = ex
		
		for file, job_ in producer.items():
			dependencies[job_].add(file)
			self.depending[job_][job].add(file)
			
		missing_input = job.missing_input - set(producer)
		if missing_input:
			include = list()
			noproducer = list()
			for f in missing_input:
				if f in exceptions:
					include.append(exceptions[f])
				else:
					noproducer.append(f)
			raise MissingInputException(job.rule, noproducer, include=include)
		
		if skip_until_dynamic:
			self._dynamic.add(job)

	def update_needrun(self, noforce = None):
		def output_mintime(job):
			for job_ in self.bfs(self.depending, job):
				t = job.output_mintime 
				if t:
					return t
		def needrun(job):
			reason = self.reason(job)
			if job != noforce and job.rule in self.forcerules or not self.forcefiles.isdisjoint(job.output):
				reason.forced = True
			elif job in self.targetjobs:
				if not job.output:
					reason.updated_input.update([f for f in job.input if not f.exists])
				else:
					if job.rule in self.targetrules:
						missing_output = job.missing_output()
					else:
						missing_output = job.missing_output(requested=set(chain(*self.depending[job].values())) | self.targetfiles)
					reason.missing_output.update(missing_output)
			else:
				output_mintime_ = output_mintime(job)
				if output_mintime_:
					updated_input = [f for f in job.input if f.exists and f.is_newer(output_mintime_)]
					reason.updated_input.update(updated_input)
			return job
		
		queue = list(filter(self.reason, map(needrun, self.jobs)))
		visited = set(queue)
		while queue:
			job = queue.pop(0)
			self._needrun.add(job)

			for job_, files in self.dependencies[job].items():
				missing_output = job_.missing_output(requested=files)
				self.reason(job_).missing_output.update(missing_output)
				if missing_output and not job_ in visited:
					visited.add(job_)
					queue.append(job_)

			for job_, files in self.depending[job].items():
				self.reason(job_).updated_input_run.update(files)
				if not job_ in visited:
					visited.add(job_)
					queue.append(job_)

		self._len = len(self._needrun)
	
	def update_priority(self):
		prioritized = lambda job: not job.rule in self.priorityrules and not self.priorityfiles.isdisjoint(job.output)
		for job in self.bfs(self.dependencies, *filter(prioritized, self.needrun_jobs), stop=self.noneedrun_finished):
			job.priority = sys.maxint
	
	def postprocess(self, noforce = None):
		self.update_needrun(noforce=noforce)
		self.update_priority()
	
	def finish(self, job, update_dynamic = True):
		self._finished.add(job)
		if update_dynamic and job.dynamic_output:
			logger.warning("Dynamically updating jobs")
			newjob = self.update_dynamic(job)
			if newjob:
				self.postprocess(noforce=newjob)
				# add 1 since the finished dynamic job was replaced by a not needrun job
				self._len += 1
	
	def update_dynamic(self, job):
		dynamic_wildcards = job.dynamic_wildcards
		if not dynamic_wildcards:
			# this happens e.g. in dryrun if output is not yet present
			return
		depending = list(filter(lambda job_: not self.finished(job_), self.bfs(self.depending, job)))
		job.rule.update_dynamic(dynamic_wildcards, input=False)
		newjob = Job(job.rule)
		self.replace_job(job, newjob)
		for job_ in depending:
			if job_.dynamic_input:
				if job_.rule.update_dynamic(dynamic_wildcards):
					if not self.dynamic(job_):
						#print(job_, job_.targetfile, job_.dynamic_output)
						newjob_ = Job(job_.rule, targetfile=job_.targetfile)
						self.replace_job(job_, newjob_)
		return newjob
	
	def delete_job(self, job, recursive=True):
		for job_ in self.depending[job]:
			del self.dependencies[job_][job]
		del self.depending[job]
		for job_ in self.dependencies[job]:
			depending = self.depending[job_]
			del depending[job]
			if not depending and recursive:
				self.delete_job(job_)
		del self.dependencies[job]
		if job in self._needrun:
			self._len -= 1
			self._needrun.remove(job)
			del self._reason[job]
		if job in self._finished:
			self._finished.remove(job)
		if job in self._dynamic:
			self._dynamic.remove(job)
	
	def replace_job(self, job, newjob):
		self.delete_job(job)
		self.update([newjob])
		if job in self.targetjobs:
			self.targetjobs.remove(job)
			self.targetjobs.add(newjob)
	
	def collect_potential_dependencies(self, job):
		dependencies = defaultdict(list)
		for file in job.input:
			try:
				for job_ in self.file2jobs(file):
					dependencies[file].append(job_)
			except MissingRuleException as ex:
				pass
		return dependencies
	
	def bfs(self, direction, *jobs, stop = lambda job: False):
		queue = list(jobs)
		visited = set(queue)
		while queue:
			job = queue.pop(0)
			if stop(job):
				# stop criterion reached for this node
				continue
			yield job
			for job_, _ in direction[job].items():
				if not job_ in visited:
					queue.append(job_)
					visited.add(job_)
	
	def dfs(self, direction, *jobs, stop = lambda job: False, post = True):
		visited = set()
		for job in jobs:
			for job_ in self._dfs(direction, job, visited, stop=stop, post=post):
				yield job_
	
	def _dfs(self, direction, job, visited, stop, post):
		if stop(job):
			return
		if not post:
			yield job
		for job_ in direction[job]:
			if not job_ in visited:
				visited.add(job_)
				for j in self._dfs(direction, job_, visited, stop, post):
					yield j
		if post:
			yield job

	def new_wildcards(self, job):
		new_wildcards = set(job.wildcards.items())
		for job_ in self.dependencies[job]:
			if not new_wildcards:
				return set()
			for wildcard in job_.wildcards.items():
				new_wildcards.discard(wildcard)
		return new_wildcards
	
	def rule2job(self, targetrule):
		return Job(rule=targetrule)
	
	def file2jobs(self, targetfile):
		jobs = list()
		for rule in self.rules:
			if rule.is_producer(targetfile):
				jobs.append(Job(rule, targetfile=targetfile))
		if not jobs:
			raise MissingRuleException(targetfile)
		return jobs
	
	def dot(self, errors = False):
		jobid = dict((job, i) for i, job in enumerate(self.jobs))
		nodes, edges = list(), list()
		types = ["running job", "not running job", "dynamic job", "error"]
		styles = ['style="rounded"', 'style="rounded,dashed"', 'style="rounded,dotted"', 'style="rounded,filled", fillcolor="red"']
		used_types = set()
		for job in self.jobs:
			label = "\\n".join([job.rule.name] + list(map(": ".join, self.new_wildcards(job))))
			t = 0
			if not self.needrun(job):
				t = 1
			if self.dynamic(job) or job.dynamic_input:
				t = 2
			used_types.add(t)
			nodes.append('\t{}[label = "{}", {}];'.format(jobid[job], label, styles[t]))
			for job_ in self.dependencies[job]:
				edges.append("\t{} -> {};".format(jobid[job_], jobid[job]))
		legend = list()
		for t in used_types:
			legend.append('\tlegend{}[label="{}", {}];'.format(t, types[t], styles[t]))
			for target in map(jobid.__getitem__, self.targetjobs):
				legend.append("\t{} -> legend{}[style=invis];".format(target, t))
				
		return textwrap.dedent("""\
		                    digraph snakemake_dag {{
		                    	graph[bgcolor=white];
		                    	node[shape=box,style=rounded];
		                    {nodes}
		                    {edges}
		                    {legend}
		                    }}\
		                    """).format(nodes="\n".join(nodes), 
		                                edges="\n".join(edges), 
		                                legend="\n".join(legend))
	
	def __str__(self):
		return self.dot()

	def __len__(self):
		return self._len
