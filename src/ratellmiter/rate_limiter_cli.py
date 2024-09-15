import argparse
import os
import subprocess
import traceback

from rate_llmiter import get_rate_limiter_monitor, DEFAULT_RATE_LIMITED_SERVICE_NAME


def ratellmiter_cli():
    get_rate_limiter_monitor().start()
    parser = argparse.ArgumentParser(description='Run specific functions from the command line.')
    parser.add_argument('-name', type=str, help='name argument')
    parser.add_argument('-file', type=str, help='file argument')
    parser.add_argument('-lines', type=str, help='ex: iroef, i=issued, r=requests, o=overflow, e=exceptions, f=finished')
    args = parser.parse_args()
    try:
        file_name = args.file
        model_name = args.name
        if model_name is None:
            model_name = DEFAULT_RATE_LIMITED_SERVICE_NAME
        lines = args.lines
        result = get_rate_limiter_monitor().graph_model_requests(file_name, model_name, lines)
        if result is not None:
            print("plot_file_name:"+result)
            current_dir = os.path.dirname(os.path.abspath(__file__))
            open_graph_path = os.path.join(current_dir, "open_graph.sh")
            open_command = str(open_graph_path) + " " + result
            subprocess.run(f"bash {open_command} &", shell=True)
        else:
            print("No data to plot")
    except Exception as e:
        stack_trace = traceback.format_exc()
        print(stack_trace)
        print(str(e))
    finally:
        get_rate_limiter_monitor().stop()
        exit(0)

if __name__ == "__main__":
    ratellmiter_cli()