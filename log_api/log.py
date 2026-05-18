import os
import logging
import requests
import urllib3

from datetime import datetime, timezone, timedelta

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def get_moscow_time():
    try:
        response = requests.get("https://yandex.com/time/sync.json?geo=213", verify=False)
        response.raise_for_status()
        data = response.json()
        moscow_time = datetime.fromtimestamp((data.get('time') / 1000),
                                             tz=timezone(timedelta(hours=3))).replace(tzinfo=None)
        return moscow_time
    except requests.exceptions.RequestException as e:
        logger.error(description=f"Ошибка при получении времени: {e}")
        return datetime.now(tz=timezone(timedelta(hours=3))).replace(tzinfo=None)


class MoscowFormatter(logging.Formatter):
    def formatTime(self, record, date_fmt=None):
        moscow_time = get_moscow_time()
        if date_fmt:
            return moscow_time.strftime(date_fmt)
        else:
            return moscow_time.isoformat()


class RemoteLogger:
    def __init__(self):
        log_dir = "log"
        os.makedirs(log_dir, exist_ok=True)

        log_file = os.path.join(log_dir, f"{get_moscow_time().strftime('%Y-%m-%d')}.log")

        self.logger = logging.getLogger("RemoteLogger")
        self.logger.setLevel(logging.INFO)

        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.INFO)

        file_handler = logging.FileHandler(log_file, encoding='utf-8')
        file_handler.setLevel(logging.INFO)

        formatter = MoscowFormatter('%(asctime)s - %(levelname)s - %(message)s')
        console_handler.setFormatter(formatter)
        file_handler.setFormatter(formatter)

        self.logger.addHandler(console_handler)
        self.logger.addHandler(file_handler)

    def error(self, description: str = '') -> None:
        self.logger.error(description)

    def info(self, description: str = '') -> None:
        self.logger.info(description)

    def warning(self, description: str = '') -> None:
        self.logger.warning(description)


logger = RemoteLogger()
