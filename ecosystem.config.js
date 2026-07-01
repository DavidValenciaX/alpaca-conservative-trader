module.exports = {
  apps: [{
    name: 'trading-bot',
    script: 'start.sh',
    cwd: '/home/ubuntu/trading_bot',
    interpreter: 'bash',
    watch: false,
    max_memory_restart: '500M',
    log_date_format: 'YYYY-MM-DD HH:mm:ss',
    error_file: '/home/ubuntu/trading_bot/logs/pm2_error.log',
    out_file: '/home/ubuntu/trading_bot/logs/pm2_out.log',
    merge_logs: true,
    autorestart: true,
    restart_delay: 5000,
  }]
};
