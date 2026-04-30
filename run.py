import uvicorn

if __name__ == "__main__":
    # reload=False reduces Windows reloader CancelledError noise
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=False)