// Runs one actions/github-script body from strix.yaml against a fake GitHub API.
// stdin: {"script": "...", "env": {...}, "state": {...}}; stdout: {"outputs", "notices", "calls", "error"}.
'use strict';

const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const state = input.state;
const calls = [];
const outputs = {};
const notices = [];

const reply = (name, data) => async (params) => {
  calls.push({ name, params });
  if (data instanceof Error) throw data;
  return { data: typeof data === 'function' ? data(params) : data };
};
const notFound = () => Object.assign(new Error('Not Found'), { status: 404 });

const github = {
  paginate: async (fn, params) => (await fn(params)).data,
  rest: {
    repos: {
      get: reply('repos.get', { private: !!state.private, default_branch: 'main' }),
      getBranch: reply('repos.getBranch', { commit: { sha: state.branchSha } }),
      compareCommitsWithBasehead: reply('repos.compareCommitsWithBasehead', state.compare === 'error' ? notFound() : state.compare),
      createCommitStatus: reply('repos.createCommitStatus', {}),
    },
    pulls: {
      get: reply('pulls.get', state.pr),
      listFiles: reply('pulls.listFiles', (state.files || []).map(filename => ({ filename }))),
    },
    issues: {
      listForRepo: reply('issues.listForRepo', state.issues || []),
      listComments: reply('issues.listComments', state.comments || []),
      createComment: reply('issues.createComment', {}),
      updateComment: reply('issues.updateComment', {}),
      removeLabel: reply('issues.removeLabel', state.labelMissing ? notFound() : {}),
      create: reply('issues.create', { number: 99 }),
      update: reply('issues.update', {}),
    },
  },
};

const core = {
  setOutput: (k, v) => { outputs[k] = String(v); },
  notice: (m) => notices.push(m),
};
const context = {
  repo: { owner: 'org', repo: 'repo' },
  serverUrl: 'https://github.com',
  runId: 42,
};

Object.assign(process.env, input.env);
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
new AsyncFunction('github', 'context', 'core', input.script)(github, context, core)
  .then(() => process.stdout.write(JSON.stringify({ outputs, notices, calls })))
  .catch((e) => process.stdout.write(JSON.stringify({ outputs, notices, calls, error: String(e) })));
