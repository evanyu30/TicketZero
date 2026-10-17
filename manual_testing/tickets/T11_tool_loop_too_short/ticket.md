# Ticket T11

Subject: Weather answers are empty, but the tool runs

Our logs show get_weather IS being called with the right city, but ask() still returns an empty string. A teammate 'optimized' the request loop to save latency. weather_card() works.
