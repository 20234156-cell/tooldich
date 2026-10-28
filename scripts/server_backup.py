"""Verify or restore an existing backup offline; restore never overwrites a directory."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['verify','restore','restore-package','create','export-drive','export-directory','drill'])
    parser.add_argument('backup',type=Path,nargs='?')
    parser.add_argument('--destination',type=Path)
    parser.add_argument('--allow-same-disk',action='store_true',help='Local replica only; never permits deleting originals')
    args=parser.parse_args()
    from src.server.backups import verify,restore
    if args.action=='create':
        parser.error('Tạo bản sao qua trang quản lý server để giữ maintenance gate cùng backend.')
    if not args.backup:parser.error('Cần đường dẫn/tên backup')
    if args.action=='restore-package':
        if not args.destination:parser.error('Cần --destination là thư mục mới ngoài dự án đang dùng')
        import tempfile
        from src.server.backup_remote import unpack
        with tempfile.TemporaryDirectory(prefix='restore_package_') as staging:
            result=restore(unpack(args.backup,Path(staging)/'package'),args.destination)
    elif args.action in {'export-drive','export-directory','drill'}:
        from src.server.backup_remote import export_drive,export_directory,drill
        if args.action!='export-drive' and not args.destination:parser.error('Cần --destination')
        result=export_drive(args.backup) if args.action=='export-drive' else export_directory(args.backup,args.destination,args.allow_same_disk) if args.action=='export-directory' else drill(args.backup.name,args.destination)
    elif args.action=='restore':
        if not args.destination:parser.error('restore requires --destination pointing to a new directory')
        result=restore(args.backup,args.destination)
    else:
        result={'verified':bool(verify(args.backup))}
    print(json.dumps(result,ensure_ascii=True))


if __name__=='__main__':main()
