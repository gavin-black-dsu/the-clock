# The Clock

This project is a fullscreen image clock created as a gift. It runs on Python and Pygame and shows the time alongside current temperature and weather information. The artwork is fully AI generated.

## Configuration

Settings are loaded from `config.json`. You can tweak brightness, colors, and endpoints for temperature and weather data. Images live under `images/<theme>/` and you can add new themes by creating additional folders.

```json
{
  "theme": "succulent",
  "brightness_day": 1.0,
  "brightness_night": 0.5,
  "temp_endpoint": "http://snek:8000/habitat/api/most_recent",
  "weather_endpoint": "http://snek:8000/weather/api/most_recent"
}
```

### Temperature endpoint
The clock expects an endpoint that returns JSON like:

```json
{ "Temperature": 72.0 }
```

### Weather endpoint
The weather API should return JSON containing the icon name used for the theme:

```json
{ "condition": "clear_day" }
```

## Vibe Coding
This code was developed entirely using [Vibe Coding](https://vibecoding.org/), a workflow focused on improvisation and live feedback.

## Screenshot
Add a screenshot here once available:

![Screenshot](images/screenshot_placeholder.png)

