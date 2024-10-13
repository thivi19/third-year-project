import requests

def discover_signposting_links(url):
    try:
        # Send a GET request to get the headers and response
        response = requests.get(url)
        
        # Print all headers to inspect them
        print("Headers:", response.headers)
        
        # Check if 'Link' header exists
        if 'Link' in response.headers:
            links = response.headers['Link']
            print("Signposting Links Found:")
            
            # Process the 'Link' headers
            for link in links.split(','):
                parts = link.split(';')
                if len(parts) > 1:
                    link_url = parts[0].strip('<>')
                    relation = parts[1].strip().split('=')[1].strip('"')
                    print(f"{relation}: {link_url}")
        else:
            print("No Signposting Links Found in Headers.")
    except requests.exceptions.RequestException as e:
        print(f"Error fetching URL: {e}")

# Example usage
url_to_check = "https://workflowhub.eu/"
discover_signposting_links(url_to_check)
