module.exports = {
  apps: [
    {
      name: 'colligator',
      cwd: '/root/colligator',
      script: 'server.js',
      node_args: '--env-file=.env',
      env: {
        PORT: '3000',
        HOST: '127.0.0.1',
        TLS: '0',
      },
    },
    {
      name: 'fotomob',
      cwd: '/root/fotomob',
      script: 'server.js',
      env: {
        PORT: '3001',
        HOST: '127.0.0.1',
        FOTOMOB_SUBFOLDER_SHARE: '0',
      },
    },
  ],
};
