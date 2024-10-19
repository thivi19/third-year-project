import requests

def discover_signposting_links(url, depth=0, max_depth=2):
    if depth > max_depth:
        return

    try:
        # Send a GET request to get the headers and response
        response = requests.get(url)
        
        # Print all headers to inspect them
        print(f"\nFetching URL: {url}")
        print("Status Code:", response.status_code)
        print("Headers:", response.headers)
        
        # Check if 'link' header exists
        if 'link' in response.headers:
            links = response.headers['link']
            print("Signposting Links Found:")
            
            # Process the 'Link' headers
            for link in links.split(','):
                parts = link.split(';')
                if len(parts) > 1:
                    link_url = parts[0].strip('<> ')  # Remove angle brackets and any trailing spaces
                    relation = parts[1].strip().split('=')[1].strip('"')
                    print(f"{relation}: {link_url}")
                    
                    # Validate the URL before making a request
                    if link_url:
                        # Fetch the linkset if it's a linkset relation
                        if 'linkset' in relation:
                            print(f"Attempting to fetch linkset from: {link_url}")  # Log the linkset URL
                            fetch_linkset(link_url)  # Call fetch_linkset with the correct URL
                        else:
                            discover_signposting_links(link_url, depth + 1, max_depth)
        else:
            print("No Signposting Links Found in Headers.")
    except requests.exceptions.RequestException as e:
        print(f"Error fetching URL: {e}")

def fetch_linkset(url):
    try:
        # Request the linkset using content negotiation
        headers = {"Accept": "application/linkset+json"}
        response = requests.get(url, headers=headers)
        
        print(f"Requesting linkset from: {url}")  # Log the request URL
        if response.status_code == 200:
            print("Linkset retrieved successfully.")
            linkset_data = response.json()  # Return the JSON response
            print("Linkset Data:", linkset_data)  # Print the entire linkset data
            return linkset_data
        else:
            print(f"Failed to retrieve linkset. Status Code: {response.status_code}")
    except requests.exceptions.RequestException as e:
        print(f"Error fetching linkset: {e}")

# Example usage
url_to_check = "https://zenodo.org/records/7977333"
discover_signposting_links(url_to_check)
