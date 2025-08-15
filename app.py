from fastapi import FastAPI
import uvicorn

app = FastAPI()

@app.get("/")
def read_root():
    return {"message": "El bot está vivo y coleando, chingón 🧟‍♂️🔥"}

if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=10000)
