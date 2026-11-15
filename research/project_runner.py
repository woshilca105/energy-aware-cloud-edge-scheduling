"""Run the current cloud-edge energy experiment and local dashboard."""
import sys
from current_project_view import main

if __name__=='__main__':
    try:
        main()
    except Exception as exc:
        print(f'运行未完成：{exc}',file=sys.stderr)
        sys.exit(1)
