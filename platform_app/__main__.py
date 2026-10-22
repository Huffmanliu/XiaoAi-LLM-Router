"""Run one local web/worker instance; production exposure requires HTTPS configuration."""
import argparse
import ipaddress
import os
from pathlib import Path
import uvicorn


def main():
    parser = argparse.ArgumentParser(description='小爱 AI 用户前台与管理后台')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--state-dir', default='deploy/local/platform')
    args = parser.parse_args()
    if args.host not in ('127.0.0.1', 'localhost', '::1'):
        if not os.environ.get('PLATFORM_PUBLIC_ORIGIN', '').startswith('https://') or os.environ.get('PLATFORM_COOKIE_SECURE') != '1':
            parser.error('对外开放须配置 HTTPS 来源和安全 Cookie；本机请用默认地址')
    trusted = os.environ.get('PLATFORM_TRUSTED_PROXIES', '')
    try:
        for address in filter(None, trusted.split(',')):
            ipaddress.ip_network(address.strip(), strict=False)
    except ValueError:
        parser.error('可信代理必须为明确的 IP 或网段，禁止使用通配符')
    if not os.environ.get('PLATFORM_PUBLIC_ORIGIN') and args.host in ('127.0.0.1', 'localhost', '::1'):
        host = '[::1]' if args.host == '::1' else args.host
        os.environ['PLATFORM_PUBLIC_ORIGIN'] = f'http://{host}:{args.port}'
    os.environ['PLATFORM_STATE_DIR'] = str(Path(args.state_dir).resolve())
    from .app import create_app
    app = create_app()
    print(f'控制台：http://{args.host}:{args.port}；首次管理员凭据见 {args.state_dir}/bootstrap-admin.txt')
    # Access URLs may include sensitive query strings. No access or raw error logs.
    # Uvicorn's default dictConfig re-enables disabled dependency loggers.
    # Preserve Runtime's suppression of raw Xiaomi/model authentication responses.
    uvicorn.run(app, host=args.host, port=args.port, workers=1, access_log=False, log_level='critical', log_config=None,
                proxy_headers=bool(trusted), forwarded_allow_ips=trusted or '', timeout_graceful_shutdown=25)


if __name__ == '__main__':
    main()
