#  Copyright © 2024 Thomas Edward Burns
#
#  Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated
#  documentation files (the “Software”), to deal in the Software without restriction, including without limitation the
#  rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to
#  permit persons to whom the Software is furnished to do so, subject to the following conditions:
#
#  The above copyright notice and this permission notice shall be included in all copies or substantial portions of the
#  Software.
#
#  THE SOFTWARE IS PROVIDED “AS IS”, WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE
#  WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR
#  COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR
#  OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
import argparse
import copy
import datetime
import functools
import json
import os
import subprocess
import threading
import time
import traceback
from pathlib import Path
import matplotlib.pyplot as plt

MIN_TEST_IF_SERVICE_RESUMED_INTERVAL = 10
MAX_TEST_IF_SERVICE_RESUMED_INTERVAL = 65
INTERVAL_BACKOFF_RATE = 1.5
RATE_LIMIT_RETRIES = 20

LOG_DIRECTORY_ENV_VAR = "RATELLMITER_LOGS"
DEFAULT_LOG_DIRECTORY = "ratellmiter_logs"
DEFAULT_RATE_LIMITED_SERVICE_NAME = "default"

class LlmClientRateLimitException(Exception):
    def __init__(self):
        super().__init__("Rate limit exceeded")
        self.status_code = 429


# For graph names, model name can include / characters, so we need to sanitize the name
def sanitize_file_name(file_name):
    result = file_name.replace("/", "-")
    return result


class RateLimitedService:
    def ratellmiter_is_llm_blocked(self) -> bool:
        raise NotImplementedError

    def get_service_name(self) -> str:
        raise NotImplementedError

    def get_ratellmiter(self, model_name:str=None) -> 'BucketRateLimiter':
        raise NotImplementedError


class DefaultRateLimitedService(RateLimitedService):
    def __init__(self, rate_limit=300):
        self.rate_limit = rate_limit if rate_limit is not None else 300
        self.rate_limiter = BucketRateLimiter(rate_limit)
        self.rate_limiter.set_rate_limited_service(self)

    def ratellmiter_is_llm_blocked(self) -> bool:
        return False

    def get_service_name(self) -> str:
        return DEFAULT_RATE_LIMITED_SERVICE_NAME

    def get_ratellmiter(self, model_name:str=None) -> 'BucketRateLimiter':
        return self.rate_limiter


class RateLimitedEvent:
    def __init__(self, second_bucket_ticket_was_issued: int, second_bucket_ticket_was_limited: int,
                 reissued_second_bucket_id: int = None):
        self.second_bucket_ticket_was_issued = second_bucket_ticket_was_issued
        self.second_bucket_ticket_was_limited = second_bucket_ticket_was_limited
        self.reissued_second_bucket_id = reissued_second_bucket_id

    def is_waiting(self):
        result = self.reissued_second_bucket_id is None
        return result

    def to_dict(self):
        result = copy.deepcopy(vars(self))
        return result

    @staticmethod
    def from_dict(dict):
        result = RateLimitedEvent(**dict)
        return result


class RateLlmiterTicket:
    def __init__(self, request_id:int, initial_request_second_bucket_id: int, user_request_id: str, model_name: str,
                 issued_ticket: int= None, issued_second_bucket_id: int = None,
                 rate_limit_event_list: [RateLimitedEvent] = None, finished_second_bucket_id: int = None):
        self.request_id = request_id
        self.initial_request_second_bucket_id = initial_request_second_bucket_id
        self.user_request_id = user_request_id
        self.model_name = model_name
        self.issued_ticket = issued_ticket
        self.issued_second_bucket_id = issued_second_bucket_id
        self.finished_second_bucket_id = finished_second_bucket_id
        self.rate_limit_event_list = rate_limit_event_list if rate_limit_event_list is not None else []

    def has_issued_ticket(self):
        result = self.issued_ticket is not None
        return result

    def record_issued_ticket(self, issued_ticket, second_bucket_id):
        self.issued_ticket = issued_ticket
        self.issued_second_bucket_id = second_bucket_id

    def add_rate_limited_event(self, second_bucket_id):
        self.issued_ticket = None
        self.issued_second_bucket_id = None
        rate_limited_event = RateLimitedEvent(self.issued_second_bucket_id, second_bucket_id)
        self.rate_limit_event_list.append(rate_limited_event)

    def resolve_rate_limited_event(self, second_bucket_id):
        self.rate_limit_event_list[-1].reissued_second_bucket_id = second_bucket_id

    def get_last_rate_limited_event(self):
        result = None
        if len(self.rate_limit_event_list) > 0:
            result = self.rate_limit_event_list[-1]
        return result

    def finish_request(self, second_bucket_id):
        self.finished_second_bucket_id = second_bucket_id

    def to_dict(self):
        result = copy.deepcopy(vars(self))
        result["rate_limit_event_list"] = [event.to_dict() for event in self.rate_limit_event_list]
        return result

    @staticmethod
    def from_dict(dict):
        dict["rate_limit_event_list"] = [RateLimitedEvent.from_dict(event_dict) for event_dict in dict["rate_limit_event_list"]]
        result = RateLlmiterTicket(**dict)
        return result


class WaitingTicket:
    def __init__(self, ticket: RateLlmiterTicket):
        self.ticket = ticket
        self.event = threading.Event()

    def wait(self):
        self.event.clear()
        self.event.wait()

    def resume_request(self):
        self.event.set()
        return self.ticket


class SecondTicketBucket:
    def __init__(self, second_bucket_id: int, ticket_count: int = 0, issued_ticket_count: int = 0,
                 issued_ticket_list: [RateLlmiterTicket] = None, second_requested_ticket_count: int = 0,
                 overflow_request_list: [RateLlmiterTicket] = None,
                 rate_limited_request_list: [RateLlmiterTicket] = None,
                 finished_ticket_list: [RateLlmiterTicket] = None):
        self.second_bucket_id = second_bucket_id
        self.second_requested_ticket_count = second_requested_ticket_count
        self.ticket_count = ticket_count
        self.issued_ticket_count = issued_ticket_count
        self.issued_ticket_list: [RateLlmiterTicket] = issued_ticket_list if issued_ticket_list is not None else []
        # requests that could not be satisfied
        self.overflow_request_list: [RateLlmiterTicket] = overflow_request_list if overflow_request_list is not None else []
        # requests that had tickets issued, but generated rate limit exceptions
        self.rate_limited_request_list: [RateLlmiterTicket] = rate_limited_request_list if rate_limited_request_list is not None else []
        self.finished_ticket_list: [RateLlmiterTicket] = finished_ticket_list if finished_ticket_list is not None else []

    def get_new_requests(self):
        result = []
        for ticket in self.overflow_request_list:
            if ticket.initial_request_second_bucket_id == self.second_bucket_id:
                result.append(copy.deepcopy(ticket))
        for ticket in self.issued_ticket_list:
            if ticket.initial_request_second_bucket_id == self.second_bucket_id:
                result.append(copy.deepcopy(ticket))
        return result

    def get_issued_tickets(self):
        result = copy.deepcopy(self.issued_ticket_list)
        return result

    def get_new_rate_exceptions(self):
        result = []
        for ticket in self.rate_limited_request_list:
            if ticket.get_last_rate_limited_event().second_bucket_ticket_was_limited == self.second_bucket_id:
                result.append(copy.deepcopy(ticket))
        return result

    def get_finished_requests(self):
        result = copy.deepcopy(self.finished_ticket_list)
        return result

    def get_ticket(self, request_id: int, user_request_id: str, model_name: str) -> RateLlmiterTicket:
        result = RateLlmiterTicket(request_id, self.second_bucket_id, user_request_id, model_name)
        self.second_requested_ticket_count += 1
        self.process_ticket_request(result)
        if result.has_issued_ticket() is False:
            self.overflow_request_list.append(result)
        return result

    def finish_request(self, ticket: RateLlmiterTicket):
        ticket.finish_request(self.second_bucket_id)
        self.finished_ticket_list.append(ticket)

    def had_activity(self):
        result = self.second_requested_ticket_count > 0 or self.issued_ticket_count > 0 or \
            len(self.finished_ticket_list) > 0 or len(self.overflow_request_list) > 0 or len(self.rate_limited_request_list) > 0
        return result

    def process_ticket_request(self, ticket):
        self.issue_ticket(ticket)
        return ticket

    def issue_ticket(self, ticket: RateLlmiterTicket):
        result = False
        if self.issued_ticket_count < self.ticket_count:
            self.issued_ticket_count += 1
            ticket.record_issued_ticket(self.issued_ticket_count, self.second_bucket_id)
            self.issued_ticket_list.append(ticket)
            result = True
        return result

    def add_rate_limited_request(self, ticket: RateLlmiterTicket):
        self.ticket_count = 0
        ticket.add_rate_limited_event(self.second_bucket_id)
        self.rate_limited_request_list.append(ticket)

    def set_ticket_count(self, max_ticket_count, min_ticket_count, prior_bucket_issued_tickets, ticket_count_delta):
        if max_ticket_count == prior_bucket_issued_tickets:
            self.ticket_count = max_ticket_count
        else:
            self.ticket_count = prior_bucket_issued_tickets + ticket_count_delta
        self.ticket_count = min(self.ticket_count, max_ticket_count)
        self.ticket_count = max(self.ticket_count, min_ticket_count)

    def transfer_tickets(self, unsatisfied_request_list: [RateLlmiterTicket],
                         rate_limited_request_list: [RateLlmiterTicket]) -> [RateLlmiterTicket]:
        released_ticket_list = []
        # requests that had tickets issued, but generated rate limit exceptions are reissued first
        for ticket in rate_limited_request_list:
            ticket = copy.deepcopy(ticket)
            if self.process_ticket_request(ticket).has_issued_ticket() is False:
                self.rate_limited_request_list.append(ticket)
            else:
                ticket.resolve_rate_limited_event(self.second_bucket_id)
                released_ticket_list.append(ticket)
        for ticket in unsatisfied_request_list:
            ticket = copy.deepcopy(ticket)
            if self.process_ticket_request(ticket).has_issued_ticket() is False:
                self.overflow_request_list.append(ticket)
            else:
                released_ticket_list.append(ticket)
        return released_ticket_list

    def get_issued_ticket_count(self):
        return self.issued_ticket_count

    def to_dict(self):
        result = copy.deepcopy(vars(self))
        result["issued_ticket_list"] = [ticket.to_dict() for ticket in self.issued_ticket_list]
        result["overflow_request_list"] = [ticket.to_dict() for ticket in self.overflow_request_list]
        result["rate_limited_request_list"] = [ticket.to_dict() for ticket in self.rate_limited_request_list]
        result["finished_ticket_list"] = [ticket.to_dict() for ticket in self.finished_ticket_list]
        return result

    @staticmethod
    def from_dict(dict):
        dict["issued_ticket_list"] = [RateLlmiterTicket.from_dict(ticket_dict) for ticket_dict in dict["issued_ticket_list"]]
        dict["overflow_request_list"] = [RateLlmiterTicket.from_dict(ticket_dict) for ticket_dict in dict["overflow_request_list"]]
        dict["rate_limited_request_list"] = [RateLlmiterTicket.from_dict(ticket_dict) for ticket_dict in dict["rate_limited_request_list"]]
        dict["finished_ticket_list"] = [RateLlmiterTicket.from_dict(ticket_dict) for ticket_dict in dict["finished_ticket_list"]]
        result = SecondTicketBucket(**dict)
        return result


class MinuteTicketBucket:
    def __init__(self, rate_limiter_name, start_time_in_seconds, iso_date_string, max_tickets_per_second, start_ramp_ticket_count,
                ramp_ticket_count_delta, minute_requested_ticket_count=0, minute_finished_request_count=0,
                 second_bucket_list: [SecondTicketBucket] = None, current_second_bucket_index=0):
        self.rate_limiter_name = rate_limiter_name
        self.iso_date_string = iso_date_string
        self.start_time_in_seconds = start_time_in_seconds
        self.max_tickets_per_second = max_tickets_per_second
        self.start_ramp_ticket_count = start_ramp_ticket_count
        self.ramp_ticket_count_delta = ramp_ticket_count_delta
        self.minute_requested_ticket_count = minute_requested_ticket_count
        self.minute_finished_request_count = minute_finished_request_count
        self.current_second_bucket_index = current_second_bucket_index
        self.second_bucket_list = second_bucket_list

    def init_second_bucket_list(self, first_bucket_ticket_count):
        self.second_bucket_list = []
        next_start_time = self.start_time_in_seconds
        for index in range(60):
            self.second_bucket_list.append(SecondTicketBucket(next_start_time))
            next_start_time += 1
        self.second_bucket_list[0].ticket_count = first_bucket_ticket_count

    def get_current_second_bucket(self):
        return self.second_bucket_list[self.current_second_bucket_index]

    def get_last_bucket_ticket_used_count(self):
        result = self.second_bucket_list[self.current_second_bucket_index].get_issued_ticket_count()
        return result

    def get_ticket(self, request_id, user_request_id, model_name) -> RateLlmiterTicket:
        result = self.second_bucket_list[self.current_second_bucket_index].get_ticket(request_id, user_request_id, model_name)
        self.minute_requested_ticket_count += 1
        return result

    def finish_request(self, ticket: RateLlmiterTicket):
        self.second_bucket_list[self.current_second_bucket_index].finish_request(ticket)
        self.minute_finished_request_count += 1

    def add_rate_limited_request(self, ticket: RateLlmiterTicket):
        self.second_bucket_list[self.current_second_bucket_index].add_rate_limited_request(ticket)

    def advance_second_bucket(self, set_ticket_count=True):
        expiring_second_bucket = self.second_bucket_list[self.current_second_bucket_index]
        self.current_second_bucket_index += 1
        self.current_second_bucket_index = min(self.current_second_bucket_index, 59)
        if set_ticket_count:
            self.second_bucket_list[self.current_second_bucket_index].set_ticket_count(self.max_tickets_per_second,
                                                                                   self.start_ramp_ticket_count,
                                                                                   expiring_second_bucket.issued_ticket_count,
                                                                                   self.ramp_ticket_count_delta)

    def release_tickets(self) -> [RateLlmiterTicket]:
        last_second_bucket = self.second_bucket_list[self.current_second_bucket_index - 1]
        result = self.second_bucket_list[self.current_second_bucket_index].transfer_tickets(last_second_bucket.overflow_request_list,
                                                        last_second_bucket.rate_limited_request_list)
        return result

    def transfer_tickets(self, last_minute_bucket) -> [RateLlmiterTicket]:
        if last_minute_bucket is None:
            return []
        else:
            last_used_second_bucket = last_minute_bucket.second_bucket_list[last_minute_bucket.current_second_bucket_index]
            result = self.second_bucket_list[0].transfer_tickets(last_used_second_bucket.overflow_request_list,
                                                        last_used_second_bucket.rate_limited_request_list)
            return result

    def to_dict(self):
        result = copy.deepcopy(vars(self))
        result["second_bucket_list"] = [bucket.to_dict() for bucket in self.second_bucket_list]
        return result

    def to_json(self):
        result = self.to_dict()
        return json.dumps(result)

    @staticmethod
    def from_dict(dict):
        dict["second_bucket_list"] = [SecondTicketBucket.from_dict(bucket_dict) for bucket_dict in dict["second_bucket_list"]]
        result = MinuteTicketBucket(**dict)
        return result


# This class is used to organize the data that needs to be locked before access
class BucketRateLimiterLock:
    def __init__(self):
        self.lock = threading.Lock()
        self.next_request_index = 0
        self.current_minute_bucket = None
        self.is_paused_by_rate_limit_exception = False
        self.waiting_ticket_dict = {}
        self.test_if_service_resumed_timer = None
        self.service_test_interval = MIN_TEST_IF_SERVICE_RESUMED_INTERVAL


class BucketRateLimiter:
    def __init__(self, request_per_minute, rate_limited_service_name:str=None,
                 rate_limited_service: RateLimitedService = None):
        self.request_per_minute = request_per_minute
        self.rate_limited_service = rate_limited_service
        # can not just ask service for name because some services use one rate limiter for many models
        self.rate_limited_service_name = rate_limited_service_name
        if rate_limited_service is not None and rate_limited_service_name is None:
            try:
                self.rate_limited_service_name = rate_limited_service.get_service_name()
            except Exception as e:
                self.rate_limited_service_name = "unknown"
        if request_per_minute < 60: # this isn't optimal, but don't really care about this use case
            self.start_ramp_ticket_count = 1
            self.max_tickets_per_second = 1
            self.ramp_ticket_count_delta = 1
        else:
            self.max_tickets_per_second = int(request_per_minute / 60)
            self.start_ramp_ticket_count = max(round((request_per_minute / 60) * 0.25), 1)
            self.ramp_ticket_count_delta = max(round((request_per_minute / 60) * 0.10), 1)
        self.thread_safe_data = BucketRateLimiterLock()
        RateLlmiterMonitor.get_instance().add_rate_limiter(self)

    def set_rate_limited_service(self, rate_limited_service: RateLimitedService):
        if self.rate_limited_service is None:  # only set once for APIs that use one rate limiter for many models
            self.rate_limited_service = rate_limited_service
            if self.rate_limited_service_name is None:
                self.rate_limited_service_name = self.rate_limited_service.get_service_name()

    def get_rate_limited_service_name(self):
        return self.rate_limited_service_name

    def get_number_of_retries(self):
        return RATE_LIMIT_RETRIES

    def get_current_minute_bucket(self):
        return self.thread_safe_data.current_minute_bucket

    def wait(self, ticket):
        waiting_ticket = WaitingTicket(ticket)
        with self.thread_safe_data.lock:
            self.thread_safe_data.waiting_ticket_dict[ticket.request_id] = waiting_ticket
        waiting_ticket.wait()
        with self.thread_safe_data.lock:
            del self.thread_safe_data.waiting_ticket_dict[ticket.request_id]
        return waiting_ticket.ticket

    def resume_request(self, ticket):
        with self.thread_safe_data.lock:
            waiting_ticket = self.thread_safe_data.waiting_ticket_dict[ticket.request_id]
            waiting_ticket.ticket = ticket # update ticket with issued ticket
        waiting_ticket.resume_request()

    def refresh_minute_bucket(self, time_in_seconds, iso_date_string):
        with self.thread_safe_data.lock:
            last_minute_bucket = self.thread_safe_data.current_minute_bucket
            if self.thread_safe_data.is_paused_by_rate_limit_exception:
                first_bucket_ticket_count = 0
            else:
                first_bucket_ticket_count = last_minute_bucket.get_last_bucket_ticket_used_count() if last_minute_bucket is not None else self.start_ramp_ticket_count
                first_bucket_ticket_count = max(first_bucket_ticket_count, self.start_ramp_ticket_count)
            self.thread_safe_data.current_minute_bucket = MinuteTicketBucket(self.rate_limited_service_name, time_in_seconds, iso_date_string, self.max_tickets_per_second,
                                                            self.start_ramp_ticket_count, self.ramp_ticket_count_delta)
            self.thread_safe_data.current_minute_bucket.init_second_bucket_list(first_bucket_ticket_count)
            released_ticket_list = self.thread_safe_data.current_minute_bucket.transfer_tickets(last_minute_bucket)
        for ticket in released_ticket_list:
            self.resume_request(ticket)
        return last_minute_bucket

    def release_tickets(self):
        with self.thread_safe_data.lock:
            last_second_bucket = self.thread_safe_data.current_minute_bucket.get_current_second_bucket()
            if self.thread_safe_data.is_paused_by_rate_limit_exception:
                self.thread_safe_data.current_minute_bucket.advance_second_bucket(False)
            else:
                self.thread_safe_data.current_minute_bucket.advance_second_bucket(True)
            released_ticket_list = self.thread_safe_data.current_minute_bucket.release_tickets()
        for ticket in released_ticket_list:
            self.resume_request(ticket)
        return last_second_bucket

    def get_ticket(self, user_request_id=None, model_name=None):
        with self.thread_safe_data.lock:
            request_id = self.thread_safe_data.next_request_index
            self.thread_safe_data.next_request_index += 1
            ticket = self.thread_safe_data.current_minute_bucket.get_ticket(request_id, user_request_id, model_name)
        if ticket.has_issued_ticket() is False:
            ticket = self.wait(ticket)
        return ticket

    def wait_for_ticket_after_rate_limit_exceeded(self, ticket):
        with self.thread_safe_data.lock:
            self.unsafe_return_ticket(ticket)
            start_service_resumed_timer = self.thread_safe_data.is_paused_by_rate_limit_exception is False
            self.thread_safe_data.is_paused_by_rate_limit_exception = True
            self.thread_safe_data.current_minute_bucket.add_rate_limited_request(ticket)
        if start_service_resumed_timer:
            self.start_test_if_service_resumed_timer()
        print(f"rate limited ticket: {ticket.request_id}")
        result = self.wait(ticket)
        print(f"reissued rate limited ticket: {ticket.request_id}")
        return result

    def return_ticket(self, ticket):
        with self.thread_safe_data.lock:
            self.unsafe_return_ticket(ticket)

    def unsafe_return_ticket(self, ticket):
        self.thread_safe_data.current_minute_bucket.finish_request(ticket)

    def start_test_if_service_resumed_timer(self):
        # this isn't thread safe, but it will only be called by one thread
        current_interval = self.thread_safe_data.service_test_interval
        timer = threading.Timer(interval=current_interval, function=self.test_if_service_resumed)
        self.thread_safe_data.test_if_service_resumed_timer = timer
        timer.start()

    def test_if_service_resumed(self):
        current_time = int(time.time())
        print("testing if blocked at: ", current_time)
        blocked = self.rate_limited_service.ratellmiter_is_llm_blocked()
        if blocked:
            self.thread_safe_data.service_test_interval = min(
                self.thread_safe_data.service_test_interval * INTERVAL_BACKOFF_RATE,
                MAX_TEST_IF_SERVICE_RESUMED_INTERVAL)
            self.start_test_if_service_resumed_timer()
        else:
            with self.thread_safe_data.lock:
                self.thread_safe_data.is_paused_by_rate_limit_exception = False
                self.thread_safe_data.test_if_service_resumed_timer = None
                self.thread_safe_data.service_test_delay = MIN_TEST_IF_SERVICE_RESUMED_INTERVAL
                # tickets released in the next second bucket


class RateLlmiterGraph:
    def __init__(self, minute_bucket_list: [MinuteTicketBucket]):
        self.minute_bucket_list = minute_bucket_list
        self.request_ticket_count_list = []
        self.tickets_issued_count_list = []
        self.overflow_ticket_count_list = []
        self.rate_exception_ticket_count_list = []
        self.finished_request_count_list = []
        self.collect_data()

    def trim_inactive_seconds(self):
        last_minute_bucket = self.minute_bucket_list[-1]
        for index in range(59, -1, -1):
            if last_minute_bucket.second_bucket_list[index].had_activity():
                break
            else:
                last_minute_bucket.second_bucket_list.pop()

    def collect_data(self):
        self.trim_inactive_seconds()
        for minute_bucket in self.minute_bucket_list:
            for second_bucket in minute_bucket.second_bucket_list:
                self.request_ticket_count_list.append(second_bucket.second_requested_ticket_count)
                self.tickets_issued_count_list.append(second_bucket.issued_ticket_count)
                self.overflow_ticket_count_list.append(len(second_bucket.overflow_request_list))
                self.rate_exception_ticket_count_list.append(len(second_bucket.rate_limited_request_list))
                self.finished_request_count_list.append(len(second_bucket.finished_ticket_list))

    def make_graph(self, plot_file_name, model_name, lines: str):
        # lines is a string that can include i, r, o, e or f for issued, requests, overflow, exceptions, finished
        plt.figure(figsize=(10, 4))

        # Plot each line with a different color
        x = range(len(self.tickets_issued_count_list))
        all_values = []
        if lines.find("i") >= 0:
            all_values.extend(self.tickets_issued_count_list)
            plt.plot(x, self.tickets_issued_count_list, label='Tickets Issued', color='green', linewidth=2, zorder=3)
        if lines.find("r") >= 0:
            all_values.extend(self.request_ticket_count_list)
            plt.plot(x, self.request_ticket_count_list, label='Requests', color='orange', linewidth=2, zorder=2)
        if lines.find("o") >= 0:
            all_values.extend(self.overflow_ticket_count_list)
            plt.plot(x, self.overflow_ticket_count_list, label='Overflow Tickets', color='blue', alpha=0.3, zorder=1)
        if lines.find("e") >= 0:
            all_values.extend(self.rate_exception_ticket_count_list)
            plt.plot(x, self.rate_exception_ticket_count_list, label='Retry Tickets', color='red', zorder=1)
        if lines.find("f") >= 0:
            all_values.extend(self.finished_request_count_list)
            plt.plot(x, self.finished_request_count_list, label='Finished Request', color='purple', alpha=0.8, zorder=1)

        # Set the title
        plt.title(f"Request Flow for {model_name}", fontsize=16, fontweight='bold')

        max_value = max(all_values)
        # Set y-axis properties
        plt.ylim(0, max_value + 2)
        if max_value < 5:
            plt.yticks(range(0, max_value + 1, max_value))
        else:
            plt.yticks(range(0, max_value + 2, max_value // 4))  # 5 ticks including 0 and max_value
        plt.gca().yaxis.set_major_formatter(plt.FuncFormatter(lambda x, p: format(int(x), ',')))

        # Set x-axis properties
        plt.xlim(-1, len(self.tickets_issued_count_list) - 1)
        max_x = len(self.tickets_issued_count_list) - 1
        ticks = [int(i * max_x / 4) for i in range(5)]
        ticks[-1] = max_x  # Ensure the last tick is always the last index
        tick_labels = [str(tick) for tick in ticks]
        plt.xticks(ticks=ticks, labels=tick_labels, fontsize=10)
        plt.gca().xaxis.set_major_formatter(plt.FuncFormatter(lambda x, p: format(int(x), ',')))

        # Add labels
        plt.xlabel('Offset in Seconds', fontsize=12)
        plt.ylabel('Number of Requests', fontsize=12)

        # Add a light gray grid that aligns with the axis ticks
        plt.grid(True, which='both', color='lightgray', linestyle='-', linewidth=0.5)

        # Align grid lines with major ticks on both x and y axes
        plt.gca().xaxis.set_major_locator(plt.MaxNLocator(integer=True))  # Ensure grid aligns with x-axis ticks
        plt.gca().yaxis.set_major_locator(plt.MaxNLocator(integer=True))  # Ensure grid aligns with y-axis ticks


        plt.legend(loc='upper right', bbox_to_anchor=(1, 1), frameon=True, fontsize=6)

        # Add a watermark
        plt.text(0.5, 0.9, 'rateLLMiter.ai',
                 fontsize=40, color='gray',
                 ha='center', va='center',
                 alpha=0.1, rotation=0,
                 transform=plt.gca().transAxes)

        plt.tight_layout()
        plt.savefig(plot_file_name, dpi=300)
        plt.close()


class CompareModelsGraph:
    def __init__(self, minute_bucket_list: [MinuteTicketBucket]):
        self.minute_bucket_list = minute_bucket_list
        self.finished_request_count_dict = {}
        self.max_value = 0
        self.collect_data()

    def collect_data(self):
        all_counts_list = []
        last_non_zero_index = 0
        for minute_bucket in self.minute_bucket_list:
            rate_limiter_finished_request_count_list = self.finished_request_count_dict.get(
                minute_bucket.rate_limiter_name)
            if rate_limiter_finished_request_count_list is None:
                rate_limiter_finished_request_count_list = []
                self.finished_request_count_dict[
                    minute_bucket.rate_limiter_name] = rate_limiter_finished_request_count_list
            for second_bucket in minute_bucket.second_bucket_list:
                count = len(second_bucket.finished_ticket_list)
                rate_limiter_finished_request_count_list.append(count)
                all_counts_list.append(count)
                if count > 0 and last_non_zero_index < (len(rate_limiter_finished_request_count_list) - 1):
                    last_non_zero_index = len(rate_limiter_finished_request_count_list) - 1
        # trim off the seconds that have no activity
        for rate_limiter_name, finished_request_count_list in self.finished_request_count_dict.items():
            if len(finished_request_count_list) > last_non_zero_index + 1:
                new_list = finished_request_count_list[:last_non_zero_index + 1]
                self.finished_request_count_dict[rate_limiter_name] = new_list
        self.max_value = max(all_counts_list)

    def shorten_rate_limiter_name(self, rate_limiter_name):
        result = rate_limiter_name
        if rate_limiter_name.find("haiku") >= 0:
            result = "haiku"
        elif rate_limiter_name.find("sonnet") >= 0:
            result = "sonnet"
        elif rate_limiter_name.find("gpt4o-mini") >= 0:
            result = "gpt4o-mini"
        elif rate_limiter_name.find("gpt4o") >= 0:
            result = "gpt4o"
        return result

    def make_graph(self, plot_file_name):
        plt.figure(figsize=(10, 4))

        default_count_list = next(iter(self.finished_request_count_dict.values()))
        x = range(len(default_count_list))
        for rate_limiter_name, finished_request_count_list in self.finished_request_count_dict.items():
            total_requests = sum(finished_request_count_list)
            rate_limiter_label = self.shorten_rate_limiter_name(rate_limiter_name) + " req:" + str(total_requests)
            plt.plot(x, finished_request_count_list, label=rate_limiter_label)
        plt.title("Finished Requests by Rate Limter", fontsize=16, fontweight='bold')

        # Set y-axis properties
        plt.ylim(0, self.max_value + 2)
        if self.max_value < 5:
            plt.yticks(range(0, self.max_value + 1, self.max_value))
        else:
            plt.yticks(range(0, self.max_value + 2, self.max_value // 4))  # 5 ticks including 0 and max_value
        plt.gca().yaxis.set_major_formatter(plt.FuncFormatter(lambda x, p: format(int(x), ',')))

        # Set x-axis properties
        plt.xlim(0, len(default_count_list) - 1)
        max_x = len(default_count_list) - 1
        ticks = [int(i * max_x / 4) for i in range(5)]
        ticks[-1] = max_x  # Ensure the last tick is always the last index
        tick_labels = [str(tick) for tick in ticks]
        plt.xticks(ticks=ticks, labels=tick_labels, fontsize=10)
        plt.gca().xaxis.set_major_formatter(plt.FuncFormatter(lambda x, p: format(int(x), ',')))

        plt.xlabel('Offset in Seconds', fontsize=12)
        plt.ylabel('Number of Requests', fontsize=12)

        # Add a light gray grid that aligns with the axis ticks
        plt.grid(True, which='both', color='lightgray', linestyle='-', linewidth=0.5)

        # Align grid lines with major ticks on both x and y axes
        plt.gca().xaxis.set_major_locator(plt.MaxNLocator(integer=True))  # Ensure grid aligns with x-axis ticks
        plt.gca().yaxis.set_major_locator(plt.MaxNLocator(integer=True))  # Ensure grid aligns with y-axis ticks

        plt.legend(loc='upper right', bbox_to_anchor=(1, 1), frameon=True, fontsize=6)
        plt.savefig(plot_file_name, dpi=300)
        plt.close()


class SecondTicketBucketListener:
    def log_second_bucket(self, bucket: SecondTicketBucket):
        raise NotImplementedError

class RateLlmiterMonitor:
    _instance = None

    def __init__(self):
        self.rate_limiter_list = []
        self.second_bucket_index = 0
        self.start_time_in_seconds = int(time.time())
        self.start_iso_date_string = datetime.datetime.fromtimestamp(self.start_time_in_seconds).isoformat()
        self.log_directory = None
        self.default_rate_limit = 300
        self.default_rate_limited_service: RateLimitedService = None
        self.active_rate_limiter_dict = {}
        self.timer = None
        self.listener_list = []
        self.configured = False
        self.started = False

    def config(self, log_directory=None, default_rate_limit=None):
        if log_directory is not None:
            self.set_log_directory(log_directory)
        if default_rate_limit is not None:
            self.set_default_rate_limit(default_rate_limit)
        self.default_rate_limited_service = DefaultRateLimitedService(self.default_rate_limit)
        if self.log_directory is None:
            self.log_directory = os.getenv(LOG_DIRECTORY_ENV_VAR)
        if self.log_directory is None:
            self.log_directory = os.path.join(os.getcwd(), DEFAULT_LOG_DIRECTORY)
        self.configured = True

    def start(self):
        if self.started is False:
            if self.configured is False:
                self.config()
            time_in_seconds = int(time.time())
            iso_date_string = datetime.datetime.fromtimestamp(time_in_seconds).isoformat()
            for rate_limiter in self.rate_limiter_list:
                rate_limiter.refresh_minute_bucket(time_in_seconds, iso_date_string)
            self.second_bucket_index = 1
            self.started = True
            self.timer = threading.Timer(interval=1, function=self.add_tickets)
            self.timer.start()


    def add_listener(self, listener: SecondTicketBucketListener):
        self.listener_list.append(listener)

    def send_bucket_to_listeners(self, bucket: SecondTicketBucket):
        for listener in self.listener_list:
            try:
                listener.log_second_bucket(bucket)
            except Exception as e:
                print(f"listener error: {e}")

    def stop(self):
        if self.timer is not None:
            buckets_to_log = []
            for rate_limiter in self.rate_limiter_list:
                buckets_to_log.append(rate_limiter.get_current_minute_bucket())
            self.write_buckets_to_log(buckets_to_log)
            self.timer.cancel()
            self.started = False

    def add_rate_limiter(self, rate_limiter):
        self.rate_limiter_list.append(rate_limiter)

    def set_log_directory(self, log_directory: str):
        self.log_directory = log_directory
        print(f"rate limiter log_directory: {self.log_directory}")
        os.makedirs(self.log_directory, exist_ok=True)

    def set_default_rate_limit(self, default_rate_limit: int):
        self.default_rate_limit = default_rate_limit

    def get_default_ratellmiter(self):
        result = self.default_rate_limited_service.get_ratellmiter()
        return result

    def add_tickets(self):
        self.second_bucket_index = self.second_bucket_index % 60
        time_in_seconds = int(time.time())
        iso_date_string = datetime.datetime.fromtimestamp(time_in_seconds).isoformat()
        buckets_to_log = []
        for rate_limiter in self.rate_limiter_list:
            if self.second_bucket_index == 0:
                last_bucket = rate_limiter.refresh_minute_bucket(time_in_seconds, iso_date_string)
                if last_bucket is not None:
                    buckets_to_log.append(last_bucket)
            else:
                last_second_bucket = rate_limiter.release_tickets()
                self.send_bucket_to_listeners(last_second_bucket)
        self.second_bucket_index += 1
        self.write_buckets_to_log(buckets_to_log)
        self.timer = threading.Timer(interval=1, function=self.add_tickets)
        try:
            self.timer.start()
        except RuntimeError as re:
            pass  # not great...but this happens as we are exiting the program

    def write_buckets_to_log(self, buckets_to_log):
        if self.log_directory is not None and len(buckets_to_log) > 0:
            log_file_name = os.path.join(self.log_directory, str(self.start_time_in_seconds) + ".jsonl")
            with open(log_file_name, "a") as file:
                for bucket in buckets_to_log:
                    if bucket.minute_requested_ticket_count > 0 or bucket.rate_limiter_name in self.active_rate_limiter_dict:
                        self.active_rate_limiter_dict[bucket.rate_limiter_name] = True
                        file.write(bucket.to_json() + "\n")
                        self.send_bucket_to_listeners(bucket.get_current_second_bucket())

    def load_session_file(self, file_name, model_name, directory=None):
        directory = directory if directory is not None else self.log_directory
        if file_name is None:
            log_directory = Path(directory)
            log_files = log_directory.glob("*.jsonl")
            non_empty_log_files = [file for file in log_files if file.stat().st_size > 0]
            session_file = max(non_empty_log_files, key=lambda f: f.stat().st_mtime, default=None)
        else:
            session_file = os.path.join(directory, file_name)
        bucket_list = []
        with open(session_file, "r") as file:
            for line in file:
                bucket_dict = json.loads(line)
                bucket = MinuteTicketBucket.from_dict(bucket_dict)
                print(f"loaded bucket: {bucket.iso_date_string}")
                bucket_list.append(bucket)
        if model_name is not None:
            bucket_list = [bucket for bucket in bucket_list if bucket.rate_limiter_name == model_name]
        file_name = os.path.basename(session_file)
        return bucket_list, file_name

    def graph_model_requests(self, file_name, model_name, lines:str, directory=None):
        lines = lines if lines is not None else "iroef" # i=issued, r=requests, o=overflow, e=exceptions, f=finished
        bucket_list, file_name = self.load_session_file(file_name, model_name, directory)
        if len(bucket_list) == 0:
            print("No data in file")
            return None
        if model_name is None:
            graph = CompareModelsGraph(bucket_list)
            plot_file_name = file_name.replace(".jsonl", "-compare.png")
            graph.make_graph(plot_file_name)
        else:
            graph = RateLlmiterGraph(bucket_list)
            plot_file_name = file_name.replace(".jsonl", "-" + sanitize_file_name(model_name) + "_" + lines + ".png")
            graph.make_graph(plot_file_name, model_name, lines)
        return plot_file_name

    @staticmethod
    def get_instance():
        if RateLlmiterMonitor._instance is None:
            RateLlmiterMonitor._instance = RateLlmiterMonitor()
        return RateLlmiterMonitor._instance


def get_rate_limiter_monitor() -> RateLlmiterMonitor:
    return RateLlmiterMonitor.get_instance()


def llmiter(user_request_id_arg=None, model_name_arg=None, debug=False):
    def decorator(func):
        @functools.wraps(func)
        def wrapper(self, *args, **kwargs):
            user_request_id=None
            model_name=None
            if user_request_id_arg is not None:
                user_request_id = kwargs.get(user_request_id_arg)
            if model_name_arg is not None:
                model_name = kwargs.get(model_name_arg)
            try:
                rate_limiter = self.get_ratellmiter(model_name)
                if debug and rate_limiter is None:
                    print(f"no rate limiter for {model_name}")
                elif debug:
                    print(f"rate limiter for {model_name} is {rate_limiter.get_rate_limited_service_name()}")
            except Exception as e:
                if debug:
                    print(f"error getting rate limiter: {e}")
                rate_limiter = None # ignore, decorated function might not have been an object or have a get_ratellmiter method
            if rate_limiter is None:
                rate_limiter = get_rate_limiter_monitor().get_default_ratellmiter()
            number_of_retries = rate_limiter.get_number_of_retries()
            ticket = None  # Initialize ticket variable
            for attempt in range(number_of_retries):
                try:
                    ticket = rate_limiter.get_ticket(user_request_id, model_name)  # Get initial ticket
                    result = func(self, *args, **kwargs)
                    rate_limiter.return_ticket(ticket)
                    break
                #except RateLimitError as re:
                    #ticket = rate_limiter.wait_for_ticket_after_rate_limit_exceeded(ticket)
                    #continue
                except Exception as e:
                    if getattr(e, "status_code", None) is not None and (e.status_code == 429 or e.status_code == 529):
                        ticket = rate_limiter.wait_for_ticket_after_rate_limit_exceeded(ticket)
                        continue
                    elif getattr(e, "code", None) is not None and (e.code == 429 or e.code == 529):
                        ticket = rate_limiter.wait_for_ticket_after_rate_limit_exceeded(ticket)
                        continue
                    else:
                        rate_limiter.return_ticket(ticket)
                        raise e
            return result
        return wrapper
    return decorator

def ratellmiter_cli():
    parser = argparse.ArgumentParser(description='Run specific functions from the command line.')
    parser.add_argument('-name', type=str, help='name argument')
    parser.add_argument('-dir', type=str, help='directory of log files')
    parser.add_argument('-file', type=str, help='file argument')
    parser.add_argument('-lines', type=str, help='ex: iroef, i=issued, r=requests, o=overflow, e=exceptions, f=finished')
    args = parser.parse_args()
    try:
        file_name = args.file
        model_name = args.name
        directory = args.dir
        if model_name is None:
            model_name = DEFAULT_RATE_LIMITED_SERVICE_NAME
        lines = args.lines
        if directory is None:
            get_rate_limiter_monitor().config()
        else:
            get_rate_limiter_monitor().config(log_directory=directory)
        result = get_rate_limiter_monitor().graph_model_requests(file_name, model_name, lines, directory)
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
    exit(0)
