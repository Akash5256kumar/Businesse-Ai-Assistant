## Run locally

Start the API so it is reachable from emulators and physical devices:

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

For physical Android testing, point the Flutter app at your machine's LAN IP:

```bash
flutter run --dart-define=BASE_URL=http://<YOUR_LAN_IP>:8000
```
