try:
    import os
    from dotenv import load_dotenv, find_dotenv

    env_file = os.environ.get("ENV_FILE", ".env")
    path = find_dotenv(filename=env_file, raise_error_if_not_found=True)
    print(f"Loading environemnt variables from {path}")
    load_dotenv(dotenv_path=path)

except Exception:
    # No file to set environment variables
    pass

from kaspr.app import app
app.main()