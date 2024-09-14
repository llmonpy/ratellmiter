import argparse
import traceback

from ratellmiter.rate_llmiter import get_rate_limiter_monitor


def llmonpy_cli():
    get_rate_limiter_monitor().start()
    parser = argparse.ArgumentParser(description='Run specific functions from the command line.')
    parser.add_argument('-name', type=str, help='name argument')
    parser.add_argument('-file', type=str, help='file argument')
    parser.add_argument('-lines', type=str, help='ex: iroef, i=issued, r=requests, o=overflow, e=exceptions, f=finished')
    args = parser.parse_args()
    try:
        file_name = args.file
        model_name = args.name
        lines = args.lines
        result = get_rate_limiter_monitor().graph_model_requests(file_name, model_name, lines)
        print("plot_file_name:"+result)
    except Exception as e:
        stack_trace = traceback.format_exc()
        print(stack_trace)
        print(str(e))
    finally:
        get_rate_limiter_monitor().stop()
        exit(0)

if __name__ == "__main__":
    llmonpy_cli()