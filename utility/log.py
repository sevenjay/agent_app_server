#!/usr/bin/env python3
import logging
import sys
import inspect
import threading
import traceback
from logging.handlers import RotatingFileHandler
from pathlib import Path

FATAL = 50
ERROR = 40
WARNING = 30
WARN = WARNING
INFO = 20
DEBUG = 10
VERBOSE = 5

# in logging module
# CRITICAL: Literal[50]
# FATAL: Literal[50]
# ERROR: Literal[40]
# WARNING: Literal[30]
# WARN: Literal[30]
# INFO: Literal[20]
# DEBUG: Literal[10]
# NOTSET: Literal[0]

logger = None

logging.VERBOSE = 5
logging.addLevelName(logging.VERBOSE, "VERBO")
logging.Logger.verbose = lambda inst, msg, *args, **kwargs: inst.log(logging.VERBOSE, msg, *args, **kwargs)
# logging.verbose = lambda msg, *args, **kwargs: logging.log(logging.VERBOSE, msg, *args, **kwargs)

logging.addLevelName(logging.CRITICAL, "FATAL")
logging.addLevelName(logging.WARNING, "WARN")

stack_level = 6
is_print_to_stdout = True   # is print to stdout, only FATAL, ERROR, WARN and INFO
is_basic_print_to_stdout = False  # is record stdout to log file


class ModuleFilter(logging.Filter):
    def filter(self, record):
        record.thread_id = threading.get_native_id()

        file_path = inspect.stack()[stack_level].filename
        record.file_id = Path(file_path).stem
        return True


# 31 red 32 green 34 blue 33 yellow 35 magenta
def colored_text(text, color_code):
    return f"\x1b[{color_code}m{text}\x1b[0m"


def LOGF(msg, *args, **kwargs):
    global stack_level, is_print_to_stdout, is_basic_print_to_stdout
    stack_level = 6
    lazy_initialLog()
    logger.critical(msg, *args, **kwargs)
    if is_print_to_stdout and not is_basic_print_to_stdout:
        print(f"{colored_text('FATAL', '91;1')}: {msg}", file=sys.stderr)


def LOGE(msg, *args, **kwargs):
    global stack_level, is_print_to_stdout, is_basic_print_to_stdout
    stack_level = 6
    lazy_initialLog()
    logger.error(msg, *args, **kwargs)
    if is_print_to_stdout and not is_basic_print_to_stdout:
        print(f"{colored_text('ERROR', 31)}: {msg}", file=sys.stderr)


def LOGException(exception, *args, **kwargs):
    global stack_level, is_print_to_stdout, is_basic_print_to_stdout
    stack_level = 6

    error_class = exception.__class__.__name__  # 取得錯誤類型
    detail = exception.args[0] if exception.args else 'detail_empty'  # 取得詳細內容
    if isinstance(detail, str):
        detail_str = detail
    else:
        class_name = detail.__class__.__name__
        detail_str = f'{class_name}: {str(detail)}'
    funcName, fileName, lineNum = "No Call Stack", "No Call Stack", 0

    cl, exc, tb = sys.exc_info()  # 取得Call Stack
    lastCallStack = traceback.extract_tb(tb)  # 取得Call Stack的資料
    if lastCallStack:
        fileName = lastCallStack[-1][0]  # 最後一筆取得發生的檔案名稱
        lineNum = lastCallStack[-1][1]  # 最後一筆取得發生的行號
        funcName = lastCallStack[-1][2]  # 最後一筆取得發生的函數名稱
    msg = f"{funcName} ({error_class}): {detail_str};\n{fileName}, line {lineNum},\n" \
          f"* * * * * * * * * * * * * * * * * * * * *\n" \
          f"{traceback.format_exc()}\n" \
          f"* * * * * * * * * * * * * * * * * * * * *\n"

    lazy_initialLog()
    logger.critical(msg, *args, **kwargs)
    if is_print_to_stdout and not is_basic_print_to_stdout:
        print(f"{colored_text('FATAL', '91;1')}: {msg}", file=sys.stderr)

    return funcName, error_class, detail_str, fileName, lineNum


def LOGW(msg, *args, **kwargs):
    global stack_level, is_print_to_stdout, is_basic_print_to_stdout
    stack_level = 6
    lazy_initialLog()
    logger.warning(msg, *args, **kwargs)
    if is_print_to_stdout and not is_basic_print_to_stdout:
        print(f"{colored_text('WARN ', 35)}: {msg}", file=sys.stdout)


def LOGI(msg, *args, **kwargs):
    global stack_level, is_print_to_stdout, is_basic_print_to_stdout
    stack_level = 6
    lazy_initialLog()
    logger.info(msg, *args, **kwargs)
    if is_print_to_stdout and not is_basic_print_to_stdout:
        print(f"{colored_text('INFO ', 32)}: {msg}", file=sys.stdout)


def LOGD(msg, *args, **kwargs):
    global stack_level
    stack_level = 6
    lazy_initialLog()
    logger.debug(msg, *args, **kwargs)


def LOGV(msg, *args, **kwargs):
    global stack_level
    stack_level = 7
    lazy_initialLog()
    logger.verbose(msg, *args, **kwargs)


def lazy_initialLog():
    global logger
    if logger is None:
        initialLog("default", "./")


def any_to_level(level: any):
    if isinstance(level, str):
        level = {"FATAL": FATAL, "ERROR": ERROR, "WARNING": WARNING, "INFO": INFO, "DEBUG": DEBUG, "VERBOSE": VERBOSE}[level.upper()]
    return level


def set_name_level(name, level):
    level = any_to_level(level)
    logger_special = logging.getLogger(name)
    logger_special.setLevel(level)


def make_file_and_stdout_handler(log_name, log_path="./", is_print_stdout=False):
    handlers = []
    if is_print_stdout:
        # in basic.log, all "%(file_id)-10s:" will be "__init__  :", so use (%(name)-10s)
        formatter1 = logging.Formatter("%(levelname)-5s: %(message)s")
        stream_handler: logging.Handler = logging.StreamHandler(sys.stdout)
        stream_handler.setFormatter(fmt=formatter1)
        stream_handler.addFilter(filter=ModuleFilter())
        handlers.append(stream_handler)

    # in basic.log, all "%(file_id)-10s:" will be "__init__  :", so use (%(name)-10s)
    formatter2 = logging.Formatter("%(asctime)s.%(msecs)03d %(process)d#%(thread_id)d %(levelname)-5s (%(name)-10s) %(message)s",
                                   datefmt='%m-%d %H:%M:%S')
    file_handler: logging.Handler = RotatingFileHandler(log_path + log_name + '.log', maxBytes=1048576 * 5, backupCount=3)
    file_handler.setFormatter(fmt=formatter2)
    file_handler.addFilter(filter=ModuleFilter())
    handlers.append(file_handler)
    return handlers


def initial_basic_log(path, basic_log_level, basic_file_name):
    handlers = make_file_and_stdout_handler(basic_file_name, path, is_print_stdout=is_basic_print_to_stdout)
    level = any_to_level(basic_log_level)

    logging.basicConfig(level=level, handlers=handlers)
    # logging.basicConfig(filename=basic_log_name, level=basic_log_level,
    #                     format="%(asctime)s.%(msecs)03d %(process)d#%(thread)x %(levelname)-5s [%(name)-10s] %(message)s",
    #                     datefmt='%m-%d %H:%M:%S')


def initialLog(name, path, level=INFO, is_print2std=True, is_basic_log=False, is_basic_print2std=False, basic_log_level=logging.INFO, is_multiprocess=False):
    global logger, is_print_to_stdout, is_basic_print_to_stdout
    is_print_to_stdout = is_print2std
    is_basic_print_to_stdout = is_basic_print2std
    if logger is None:  # @todo #multi-module use in one process, multi-process used ok
        import os
        process_id = os.getpid()
        os.makedirs(path, exist_ok=True)

        file_name = f"{name}_{process_id}" if is_multiprocess else name
        file_name_basic = f"{name}_basic_{process_id}" if is_multiprocess else f"{name}_basic"
        if is_basic_log:
            initial_basic_log(path, basic_log_level, file_name_basic)
        logger = logging.getLogger(name)
        level = any_to_level(level)
        logger.setLevel(level)
        logger.addFilter(ModuleFilter())

        # in name.log, all ":%(name)-10s" will be the same name
        formatter = logging.Formatter("%(asctime)s.%(msecs)03d %(process)d#%(thread_id)d %(levelname)-5s [%(file_id)-10s] %(message)s",
                                      datefmt='%m-%d %H:%M:%S')
        fileHandler = RotatingFileHandler(path + file_name + '.log', maxBytes=1048576 * 5, backupCount=3)
        fileHandler.setFormatter(formatter)

        logger.addHandler(fileHandler)

        # if is_print_to_stdout:
        #     consoleHandler = logging.StreamHandler(sys.stdout)
        #     consoleHandler.setFormatter(formatter)
        #     logger.addHandler(consoleHandler)

    else:
        if name != "default":
            logger.warning(f"already Initial Log, but name is not {name}")


def reset_log(name, path, level=INFO, is_print2std=True, is_basic_log=False, is_basic_print2std=False, basic_log_level=logging.INFO, is_multiprocess=True):
    # reduce forked multi-process log parent duplicate in child log
    global logger
    if logger is not None:
        for handler in logger.handlers[:]:
            logger.removeHandler(handler)
        logger = None
    root_logger = logging.getLogger()
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    initialLog(name, path, level, is_print2std, is_basic_log, is_basic_print2std, basic_log_level, is_multiprocess)


if __name__ == '__main__':
    initialLog("testtest", "./", logging.DEBUG)
    LOGF("CRITICAL %d" % logging.CRITICAL)
    LOGE("ERROR %d" % logging.ERROR)
    LOGW("WARNING %d" % logging.WARNING)
    LOGI("INFO %d" % logging.INFO)
    LOGD("DEBUG %d" % logging.DEBUG)
    LOGV("VERBOSE %d" % logging.VERBOSE)
    LOGI("-------------------------------")
    LOGF("CRITICAL {0}".format(logging.CRITICAL))
    LOGE("{0} {1}".format("Error", logging.ERROR))
    LOGW("{} {}".format("logging.WARNING", logging.WARNING))
    LOGI("INFO %d", logging.INFO)
    LOGD("%s %d", "Debug", logging.DEBUG)
    LOGV("%s %d" % ("Verbose", logging.VERBOSE))
    import time
    time.sleep(1)
    try:
        a = 1 / 0
    except Exception as e:
        LOGException(e)

# @reference log real thread id in python
# https://stackoverflow.com/questions/28050451/elegant-way-to-make-logging-loggeradapter-available-to-other-modules
# https://ephrain.net/python-%E5%9C%A8-python-%E7%A8%8B%E5%BC%8F%E4%B8%AD%E5%8F%96%E5%BE%97-thread-id/
