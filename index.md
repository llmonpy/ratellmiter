# rateLLMiter
<p align="center">
  <img src="artifacts/FIREWORKS_ri.png" alt="rateLLMiter smooths out requests">
</p>
rateLLMiter is a Python rate limiter that smoothes out requests to LLM APIs to get faster, more consistent performance. If a
LLM client generates too many rate limit exceptions, a LLM server is likely to throttle the client. rateLLMiter prevents
throttling by:

>1. Spreading out requests smoothly over an entire minute (vs making all requests at the beginning of the minute).  
>2. Ramps up requests over several seconds whenever there is a sudden increase in requests.  This prevents rate limit exceptions.
>3. After a rate limit exception, rateLLMiter periodically tests the LLM server to see if it is accepting requests again.
    When it is accepting requests, rateLLMiter releases the requests that had rate limit exceptions first.  

You can learn how to install and use [rateLLMiter at the repo](https://github.com/llmonpy/ratellmiter)
