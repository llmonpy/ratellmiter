import concurrent
import random
import threading
import time

from ratellmiter.rate_llmiter import get_rate_limiter_monitor, llmiter, BucketRateLimiter, \
    LlmClientRateLimitException, RateLimitedService

TEST_DURATION = 180 # seconds
MAX_REQUEST = 10
THREAD_POOL_SIZE = 1
NUMBER_OF_REQUESTS_IN_TEST = int((TEST_DURATION * MAX_REQUEST) / 3)


class TooManyRequestException(Exception):
    pass


class TestSecondBucket:
    def __init__(self, max_request: int, throw_rate_exception: bool):
        self.max_request = max_request
        self.current_request = 0
        self.throw_rate_exception = throw_rate_exception
        print("throw_rate_exception:"+str(throw_rate_exception))

    def add_request(self):
        result = None
        if self.throw_rate_exception:
            print("throwing simulated rate exception")
            raise LlmClientRateLimitException()
        self.current_request += 1
        if self.current_request > self.max_request:
            raise TooManyRequestException()


class TestRateLimitedService(RateLimitedService):
    def __init__(self, duration: int, max_request:int, rate_exception_buckets:[], rate_limiter: 'BucketRateLimiter'):
        self.duration = duration
        self.max_request = max_request
        self.rate_limiter = rate_limiter
        if self.rate_limiter is not None:
            rate_limiter.set_rate_limited_service(self)
        self.second_bucket_list = []
        for i in range(duration):
            throw_rate_exception = i in rate_exception_buckets
            self.second_bucket_list.append(TestSecondBucket(self.max_request, throw_rate_exception))
        self.start_time: float = None
        self.bucket_lock = threading.Lock()

    def start(self):
        self.start_time = time.time()

    @llmiter()
    def make_request(self):
        current_time = time.time()
        second_index = int(current_time - self.start_time)
        if second_index >= self.duration:
            raise ValueError("Exceeded duration")
        with self.bucket_lock:
            self.second_bucket_list[second_index].add_request()
        print(".")

    def ratellmiter_is_llm_blocked(self) -> bool:
        return False

    def get_service_name(self) -> str:
        return "RateLimitedService"

    def get_ratellmiter(self, model_name:str = None) -> 'BucketRateLimiter':
        return self.rate_limiter


class TestRateLimiter:
    def __init__(self):
        self.thread_pool = concurrent.futures.ThreadPoolExecutor(max_workers=THREAD_POOL_SIZE)
        self.rate_limiter = BucketRateLimiter(MAX_REQUEST * 60, "RateLimitedService")
        self.rate_limited_service = TestRateLimitedService(TEST_DURATION, MAX_REQUEST, [30,75], self.rate_limiter)

    def run_test(self):
        get_rate_limiter_monitor().start()
        self.rate_limited_service.start()
        future_list = []
        for i in range(NUMBER_OF_REQUESTS_IN_TEST):
            future = self.thread_pool.submit(self.call_service)
            future_list.append(future)
        error_count = 0
        for future in concurrent.futures.as_completed(future_list):
            try:
                future.result()
            except Exception as e:
                print("llmiter didnt work")
                error_count += 1
                print(str(e))
        get_rate_limiter_monitor().stop()
        print("done: error_count:"+str(error_count))

    def call_service(self):
        self.rate_limited_service.make_request()


if __name__ == "__main__":
    test_rate_limiter = TestRateLimiter()
    test_rate_limiter.run_test()
    get_rate_limiter_monitor().stop()
    exit(0)
