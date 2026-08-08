# Plan: add rate limiting to the API

We need to stop abusive clients from hammering the endpoints.

Add a rate limiter to the public API. It should be efficient and shouldn't slow
down normal users. Limit clients to a reasonable number of requests.

Store the counters somewhere fast. When a client goes over the limit, reject
the request. Make sure the whole thing is robust under load.

We should probably also clean up the old throttling code while we're in there.
