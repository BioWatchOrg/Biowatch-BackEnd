module.exports = {
  extends: ['@commitlint/config-conventional'],
  parserPreset: {
    parserOpts: {
      headerPattern: /^(feat|fix|perf|refactor|test|docs|style|ci|build): (.+)$/,
      headerCorrespondence: ['type', 'subject'],
    },
  },
  rules: {
    'type-enum': [
      2,
      'always',
      ['feat', 'fix', 'perf', 'refactor', 'test', 'docs', 'style', 'ci', 'build'],
    ],
    'type-empty': [2, 'never'],
    'scope-empty': [2, 'always'],
    'subject-case': [0],
    'subject-empty': [2, 'never'],
  },
};
