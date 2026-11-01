"""Maintenance CLI for Shepherd dependencies and capability inspection."""
import argparse
import json
import os

from . import claude_sdk
from .capabilities import matrix


def main(argv=None):
    parser = argparse.ArgumentParser(prog='shepherd')
    sub = parser.add_subparsers(dest='command', required=True)
    status = sub.add_parser('status')
    status.add_argument('--offline', action='store_true',
                        help='Read connector declarations without starting engine protocol probes')
    sub.add_parser('prepare-claude')
    trial = sub.add_parser('trial-policy', help='Inspect or pause the isolated unified-storage trial')
    trial.add_argument('--data-home', default=os.environ.get('DESKTRON_DATA_HOME',
                         os.path.expanduser('~/.local/share/desktron/data')))
    trial.add_argument('--create', choices=('on','off'))
    trial.add_argument('--execute', choices=('on','off'))
    history = sub.add_parser('history', help='Read verified public turns (legacy paging)')
    history.add_argument('conversation')
    history.add_argument('--root', default=os.path.join(os.environ.get('DESKTRON_DATA_HOME',
                        os.path.expanduser('~/.local/share/desktron/data')), 'shepherd'))
    history.add_argument('--start', type=int, default=1)
    history.add_argument('--limit', type=int, default=10)
    retrieve=sub.add_parser('retrieve',help='Discover, search and read verified Shepherd history')
    from .history_cli import configure
    configure(retrieve)
    args = parser.parse_args(argv)
    if args.command == 'trial-policy':
        from .trial import policy, set_policy
        value=policy(args.data_home)
        if args.create is not None or args.execute is not None:
            value=set_policy(args.data_home, create=(args.create=='on') if args.create else value['create'],
                             execute=(args.execute=='on') if args.execute else value['execute'])
        print(json.dumps(value,ensure_ascii=False))
        return 0
    if args.command == 'retrieve':
        from .history_cli import run
        return run(args)
    if args.command == 'history':
        if args.start < 1 or not 1 <= args.limit <= 100:
            parser.error('history requires start >= 1 and limit between 1 and 100')
        from .history_retrieval import HistoryRetrieval
        page=HistoryRetrieval(args.root).read(args.conversation,start=args.start,
            end=args.start+args.limit-1,max_turns=args.limit,max_tokens=200000,
            max_bytes=2*1024*1024,max_items=1000)
        groups={}
        for record in page['records']:
            row=groups.setdefault(record['seq'],{'seq':record['seq'],
                'outcome':record['outcome'],'public':[]})
            block='input_text' if record['role']=='user' else 'output_text'
            row['public'].append({'type':'message','role':record['role']
                if record['role'] in ('user','assistant') else 'assistant',
                'content':[{'type':block if record['role']!='tool' else 'output_text',
                            'text':record['text']}]})
        total=page['snapshot']['head_seq'];end=min(total,args.start+args.limit-1)
        print(json.dumps({'conversation':args.conversation,'total':total,
            'turns':list(groups.values()),
            'next':end+1 if not page['truncated'] and end<total else None,
            'next_cursor':page['next_cursor'],'truncated':page['truncated'],
            'usage':page['usage']},ensure_ascii=False))
        return 0
    if args.command == 'prepare-claude':
        ok, detail = claude_sdk.ensure()
        print(json.dumps({'ok': ok, 'sdk': detail}, ensure_ascii=False))
        return 0 if ok else 1
    print(json.dumps({'connectors': matrix(probe=not args.offline)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
