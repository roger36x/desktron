"""CLI contract for Shepherd verified history retrieval."""
import argparse
import json
import sqlite3
import os

from .history_retrieval import HistoryError, HistoryRetrieval
from .store import Conflict, InvalidState
from .history_safety import redact_history_text

ACTIONS = frozenset(('catalog','search','read','around','verify'))


def configure(parser):
    actions=parser.add_subparsers(dest='history_action',required=True)
    common=argparse.ArgumentParser(add_help=False)
    common.add_argument('--root',default=os.path.join(os.environ.get('DESKTRON_DATA_HOME',
                        os.path.expanduser('~/.local/share/desktron/data')),'shepherd'))
    catalog=actions.add_parser('catalog',parents=[common],help='List archive heads and index state')
    search=actions.add_parser('search',parents=[common],help='Find candidate citations')
    search.add_argument('conversation')
    search.add_argument('--query',required=True)
    search.add_argument('--role',choices=('user','assistant','tool'),default='')
    search.add_argument('--limit',type=int,default=10)
    search.add_argument('--sort',choices=('relevance','time'),default='relevance')
    read=actions.add_parser('read',parents=[common],help='Read verified history within budgets')
    read.add_argument('conversation');read.add_argument('--start',type=int,default=1)
    read.add_argument('--end',type=int);read.add_argument('--cursor',default='')
    _budgets(read)
    around=actions.add_parser('around',parents=[common],help='Read around a stable turn sequence')
    around.add_argument('conversation');around.add_argument('--seq',type=int,required=True)
    around.add_argument('--before',type=int,default=3);around.add_argument('--after',type=int,default=3)
    _budgets(around,turns=False)
    verify=actions.add_parser('verify',parents=[common],help='Revalidate a history citation')
    verify.add_argument('citation')
    return parser


def _budgets(parser,turns=True):
    if turns:parser.add_argument('--max-turns',type=int,default=10)
    parser.add_argument('--max-tokens',type=int,default=8000)
    parser.add_argument('--max-bytes',type=int,default=128*1024)
    parser.add_argument('--max-items',type=int,default=100)


def run(args):
    try:
        retrieval=HistoryRetrieval(args.root)
        if args.history_action=='catalog':result=retrieval.catalog()
        elif args.history_action=='search':
            result=retrieval.search(args.query,conversation=args.conversation,
                role=args.role,limit=args.limit,sort=args.sort)
        elif args.history_action=='read':
            result=retrieval.read(args.conversation,start=args.start,end=args.end,
                cursor=args.cursor,max_turns=args.max_turns,max_tokens=args.max_tokens,
                max_bytes=args.max_bytes,max_items=args.max_items)
        elif args.history_action=='around':
            result=retrieval.around(args.conversation,args.seq,before=args.before,
                after=args.after,max_tokens=args.max_tokens,max_bytes=args.max_bytes,
                max_items=args.max_items)
        else:result=retrieval.verify(args.citation)
        print(json.dumps({'ok':True,**result},ensure_ascii=False,separators=(',', ':')))
        return 0
    except HistoryError as exc:
        print(json.dumps({'ok':False,'error':{'code':exc.code,'message':redact_history_text(str(exc))}},ensure_ascii=False))
        return 2
    except (Conflict,InvalidState,ValueError,KeyError,TypeError,OSError,sqlite3.Error) as exc:
        print(json.dumps({'ok':False,'error':{'code':'history_unavailable',
            'message':redact_history_text(str(exc))[:300]}},ensure_ascii=False))
        return 2
