import os

# the test client talks to host "testserver"; production defaults only allow localhost
os.environ.setdefault("ALLOWED_HOSTS", "localhost,127.0.0.1,testserver")
