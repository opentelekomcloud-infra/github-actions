FROM python:3.11-slim

# Install dependencies
COPY requirements.txt .
RUN pip install -r requirements.txt

# Copy the action script
COPY merge_pr.py /merge_pr.py

# Set the entrypoint to your action
ENTRYPOINT ["python", "/merge_pr.py"]
