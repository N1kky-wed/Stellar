import requests
from bs4 import BeautifulSoup
import re

def scrape_url(url):
    headers = {'User-Agent': 'Mozilla/5.0 (compatible; MyScraper/1.0)'}
    try:
        # Added a timeout for robustness
        response = requests.get(url, headers=headers, timeout=15)

        # Raise an exception for bad status codes
        response.raise_for_status()

        html = response.text
        soup = BeautifulSoup(html, 'html.parser')

        title = soup.title.string.strip() if soup.title and soup.title.string else "No title found"
        
        # Combine all paragraphs into a single string
        paragraphs = " ".join([p.get_text(strip=True) for p in soup.find_all('p')])
        
        # Use regex to find only alphanumeric characters and spaces from the combined text
        clean_text = re.sub(r'[^a-zA-Z0-9\s]', '', paragraphs)
        # Consolidate multiple spaces into one
        clean_text = re.sub(r'\s+', ' ', clean_text).strip()

        # Clean the title separately
        clean_title = re.sub(r'[^a-zA-Z0-9\s]', '', title)
        clean_title = re.sub(r'\s+', ' ', clean_title).strip()

        print("Clean Title:", clean_title)
        print("\nClean Paragraphs:")
        
        # Return the cleaned text
        return f"context from {url}: is \n{clean_text}"

    except requests.exceptions.Timeout:
        return "The request took too long and timed out."

    except requests.exceptions.RequestException as e:
        return f"An error occurred while fetching the URL: {e}"
    
    except Exception as e:
        return f"An unexpected error occurred: {e}"
