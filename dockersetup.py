# dockersetup.py (Corrected and Hardened Version)
import os
import sys
import subprocess
import shutil
# --- Configuration ---
PROJECT_ROOT = os.getcwd()
DOCKERFILES_SUBFOLDER = "dockerfiles"
TARGET_DIRECTORY = os.path.join(PROJECT_ROOT, DOCKERFILES_SUBFOLDER)
DOCKER_EXECUTABLE = shutil.which("docker") # Use the absolute path to Docker

# --- Dockerfile Content Definitions ---
DOCKERFILES_TO_CREATE = {
    "Dockerfile.py": """# Python sandbox with all required packages
FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y curl && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir matplotlib pandas numpy scipy google-genai scikit-learn Pillow requests beautifulsoup4 lxml Flask Flask-Session werkzeug python-dotenv PyPDF2 pypandoc google-generativeai google-api-core tavily-python sqlitecloud ultralytics opencv-python-headless
""",
    "Dockerfile.c": """# C sandbox
FROM gcc:latest
WORKDIR /app
""",
    "Dockerfile.cpp": """# C++ sandbox
FROM gcc:latest
WORKDIR /app
""",
    "Dockerfile.node": """# Node.js sandbox
FROM node:22-alpine
RUN npm install -g typescript
WORKDIR /app
""",
    "Dockerfile.java": """# Java sandbox
FROM eclipse-temurin:17-jdk-alpine
WORKDIR /app
""",
    "Dockerfile.go": """# Go sandbox
FROM golang:1.21-alpine
WORKDIR /app
""",
    "Dockerfile.rust": """# Rust sandbox
FROM rust:1.70-alpine
WORKDIR /app
""",
    "Dockerfile.php": """# PHP sandbox
FROM php:8.2-alpine
WORKDIR /app
""",
    "Dockerfile.ruby": """# Ruby sandbox
FROM ruby:3.2-alpine
WORKDIR /app
""",
    "Dockerfile.repo": """# Stellar Repo Host Runtime
FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y \\
    curl \\
    git \\
    wget \\
    build-essential \\
    procps \\
    iputils-ping \\
    ripgrep \\
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \\
    && apt-get install -y nodejs \\
    && npm install -g @google/gemini-cli \\
    && rm -rf /var/lib/apt/lists/*
CMD ["tail", "-f", "/dev/null"]
""",
    "Dockerfile.lab": """# Stellar Lab Core Sandbox
FROM python:3.12-slim
WORKDIR /lab
# Install common system tools, git, nodejs, and build essentials so the agent can build its own tools
RUN apt-get update && apt-get install -y \\
    curl \\
    git \\
    wget \\
    build-essential \\
    procps \\
    iputils-ping \\
    ripgrep \\
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \\
    && apt-get install -y nodejs \\
    && rm -rf /var/lib/apt/lists/*

# Keep container running indefinitely so we can exec into it
CMD ["tail", "-f", "/dev/null"]
"""
}

# --- Docker Image Definitions ---
IMAGES_TO_BUILD = {
    "Dockerfile.py": "stellar-python-sandbox:3.12",
    "Dockerfile.c": "stellar-c-sandbox:latest",
    "Dockerfile.cpp": "stellar-cpp-sandbox:latest",
    "Dockerfile.node": "stellar-node-sandbox:latest",
    "Dockerfile.java": "stellar-java-sandbox:latest",
    "Dockerfile.go": "stellar-go-sandbox:latest",
    "Dockerfile.rust": "stellar-rust-sandbox:latest",
    "Dockerfile.php": "stellar-php-sandbox:latest",
    "Dockerfile.ruby": "stellar-ruby-sandbox:latest",
    "Dockerfile.repo": "stellar-repo-host:latest",
    "Dockerfile.lab": "stellar-lab-core:latest",
}

def check_docker_dependencies():
    """Checks if Docker is installed and the Docker daemon is running."""
    print("--- Verifying Docker Environment ---")
    try:
        subprocess.run([DOCKER_EXECUTABLE, "--version"], check=True, capture_output=True)
        print("[OK] 'docker' command is available.")
        subprocess.run([DOCKER_EXECUTABLE, "info"], check=True, capture_output=True)
        print("[OK] Docker daemon is running.")
        return True
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        if "daemon" in str(e).lower():
            print("[ERROR] Error: Docker daemon is not running.")
            print("   Please start the Docker service (sudo systemctl start docker) and run this script again.")
        else:
            print(f"[ERROR] Error: '{DOCKER_EXECUTABLE}' command not found.")
            print("   Please ensure Docker is installed correctly.")
        return False

def create_dockerfiles():
    """Creates the Dockerfile blueprints in the target subfolder."""
    print("\n--- Phase 1: Creating Dockerfile Blueprints ---")
    try:
        os.makedirs(TARGET_DIRECTORY, exist_ok=True)
        print(f"Ensuring dockerfiles exist in: {TARGET_DIRECTORY}")
    except OSError as e:
        print(f"[ERROR] Error: Could not create directory {TARGET_DIRECTORY}. Details: {e}")
        return False

    for filename, content in sorted(DOCKERFILES_TO_CREATE.items()):
        full_path = os.path.join(TARGET_DIRECTORY, filename)
        with open(full_path, 'w', newline='\n') as f: f.write(content.strip())
    
    print("[OK] All Dockerfile blueprints are in place.")
    return True

def manage_images(force_rebuild=False):
    """Checks for, builds, and offers to update Docker images."""
    print("\n--- Phase 2: Managing Docker Images ---")
    
    try:
        result = subprocess.run([DOCKER_EXECUTABLE, "images", "--format", "{{.Repository}}:{{.Tag}}"], capture_output=True, text=True, check=True)
        local_images = set(result.stdout.strip().split('\n'))
    except subprocess.CalledProcessError as e:
        print(f"[ERROR] Error getting local Docker images list: {e.stderr}")
        return False

    missing_images = []
    found_images_count = 0
    for dockerfile, image_tag in sorted(IMAGES_TO_BUILD.items()):
        if image_tag in local_images:
            print(f"[OK] Found: '{image_tag}'")
            found_images_count += 1
        else:
            print(f"[WARN] Missing: '{image_tag}'")
            missing_images.append((dockerfile, image_tag))
    
    images_to_build_list = list(missing_images)
    
    if not missing_images and found_images_count > 0:
        if force_rebuild:
            print("\n--force-rebuild flag was used. Rebuilding all images.")
            images_to_build_list = list(IMAGES_TO_BUILD.items())
        else:
            print("\n[OK] All required Docker images already exist.")
            choice = input("   Do you want to force an update to apply the latest changes? (y/n): ").lower().strip()
            if choice in ['y', 'yes']:
                print("\nForcing update as requested...")
                images_to_build_list = list(IMAGES_TO_BUILD.items())
            else:
                print("Skipping update. No changes made.")
                return True

    if not images_to_build_list:
        print("\n[SUCCESS] All required Docker images are up-to-date!")
        return True
    
    print(f"\n--- [BUILD] Building/Updating {len(images_to_build_list)} Image(s) ---")
    for dockerfile, image_tag in images_to_build_list:
        print("-" * 60)
        print(f"Building '{image_tag}'... (This may take several minutes)")
        print("-" * 60)
        
        build_cmd = [DOCKER_EXECUTABLE, "build", "-f", os.path.join(DOCKERFILES_SUBFOLDER, dockerfile), "-t", image_tag, "--pull", "."]
        
        process = subprocess.Popen(build_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace')
        for line in iter(process.stdout.readline, ''):
            sys.stdout.write(line)
        
        process.wait()
        
        if process.returncode != 0:
            print(f"\n[ERROR] Error building '{image_tag}'. Build failed with code {process.returncode}.")
            return False
        else:
            print(f"\n[OK] Successfully built '{image_tag}'")
            
    return True

def main():
    """Main function to orchestrate the setup."""
    print("--- Starting Full Automated Docker Setup for Stellar ---")
    
    force_rebuild = '--force-rebuild' in sys.argv
    
    if not check_docker_dependencies(): return
    if not create_dockerfiles(): return
    if not manage_images(force_rebuild=force_rebuild): return
        
    print("\n" + "="*60)
    print(">> SETUP COMPLETE! All Docker sandbox images are ready. <<")
    print("="*60)

if __name__ == "__main__":
    main()
