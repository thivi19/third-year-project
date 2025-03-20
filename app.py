from flask import Flask, render_template, request, jsonify, redirect, url_for
import requests
from bs4 import BeautifulSoup # For parsing HTML content to extract data
import rdflib
from rdflib import Graph, URIRef, Literal, Namespace
from rdflib.namespace import RDF, RDFS, FOAF, DC, XSD, DCTERMS # Common RDF namespace definitions
import uuid
import json
import re
import logging
from SPARQLWrapper import SPARQLWrapper, JSON
from urllib.parse import urlparse, unquote # For URL parsing and character unescaping
import os
import datetime
from markupsafe import Markup
import time
from concurrent.futures import ThreadPoolExecutor
import traceback
from requests.exceptions import Timeout, RequestException
import threading


# Try to import optional modules
try:
    from rdflib_microdata import MicrodataParser
    HAS_MICRODATA = True
except ImportError:
    HAS_MICRODATA = False

app = Flask(__name__)

# Default Configuration
app.config['FUSEKI_ENDPOINT'] = 'http://localhost:3030'
app.config['FUSEKI_DATASET'] = 'knowledge_graph'
app.config['MAX_CRAWL_DEPTH'] = 3 # Limit how deep the crawler will traverse
app.config['MAX_RESOURCES_PER_LEVEL'] = 5  # Limit resources processed at each depth level
app.config['MAX_RESOURCES'] = 500  # Overall resource limit for the entire crawl
app.config['MAX_TRIPLES'] = 10000   # Maximum number of RDF triples to collect
app.config['RELEVANCE_THRESHOLD'] = 0.5  # Minimum score to consider a resource relevant
app.config['CRAWL_TIMEOUT'] = 300  # Maximum crawl duration in seconds
app.config['USE_PARALLEL'] = False  # Enable/disable parallel processing
app.config['MAX_WORKERS'] = 5  # Number of parallel worker threads when enabled

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialise the main RDF graph 
graph = Graph()

# Define and bind common RDF namespaces for more readable output
schema = Namespace("http://schema.org/")
dcat = Namespace("http://www.w3.org/ns/dcat#")
prov = Namespace("http://www.w3.org/ns/prov#")
void = Namespace("http://rdfs.org/ns/void#") # Vocabulary of Interlinked Datasets
graph.bind("schema", schema)
graph.bind("dcat", dcat)
graph.bind("prov", prov)
graph.bind("dc", DC)
graph.bind("foaf", FOAF)
graph.bind("void", void)


# In-memory state management for the crawling process
# This tracks progress, statistics, and collected data
crawl_state = {
    'visited_urls': set(),  # URLs already processed
    'current_depth': 0,
    'crawl_id': None,  # Unique identifier for this crawl
    'start_time': None,
    'crawl_active': False,
    'resource_scores': {},  # Store relevance scores for resources
    'signposting_stats': {
        'found': 0,  # Number of signposting links found
        'fallback_used': 0  # Number of times fallback discovery was used
    },
    'provenance': {}   # Detailed provenance information
}

def reset_crawl_state():
    """Reset the crawl state for a new crawl"""
    global crawl_state
    timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    crawl_id = f"crawl_{timestamp}"
    crawl_state = {
        'visited_urls': set(),
        'current_depth': 0,
        'crawl_id': crawl_id,
        'start_time': datetime.datetime.now(),
        'crawl_active': True,
        'resource_scores': {},  # Reset resource relevance scores
        'last_triples_count': 0,
        'prev_triples_count': 0,
        'no_progress_count': 0,  # Count iterations with no new triples
        'recent_logs': [],
        'current_level_urls': [],  # To track URLs at current level
        'last_status_check': datetime.datetime.now(),  # For incremental progress display
        'last_visited_count': 0,  # Track newly visited URLs
        'signposting_stats': {
            'found': 0,
            'fallback_used': 0,
            'rel_types': {},  # Track relation types found
            'mime_types': {}   # Track MIME types found
        },
        'provenance': {   # Reset provenance information
            'id': crawl_id,
            'started': datetime.datetime.now().isoformat(),
            'seed_urls': [],
            'finished': None,
            'resources_visited': 0,
            'triples_collected': 0,
            'domains_visited': set()
        }
    }
    return crawl_id


def get_signposting_links(url):
    """
    Extract signposting links from HTTP headers and HTML.
    Returns a dictionary of relation types and their target URLs.
    """
    links = {}
    
    try:
        # Get HTTP headers with Accept header
        headers = {
            'Accept': 'application/rdf+xml, text/turtle, application/ld+json, text/n3, application/n-triples',
            'User-Agent': 'FAIR-Signposting-Crawler/1.0 (Mozilla Compatible)'
        }
        # HEAD request to efficiently check headers without downloading content
        response = requests.head(url, allow_redirects=True, timeout=10, headers=headers)
        
        # Check if the response includes Link headers (primary signposting method)
        if 'Link' in response.headers:
            link_header = response.headers['Link']
            # Parse Link header using regex to extract target URLs and relation types
            link_matches = re.findall(r'<([^>]*)>\s*;\s*rel=(?:"([^"]*)"|([^,\s]*))', link_header)
            for target, rel1, rel2 in link_matches:
                # rel1 captures quoted relation values, rel2 captures unquoted values
                rel = rel1 if rel1 else rel2
                links[rel] = target
                logger.info(f"Found signposting in header: {rel} -> {target}")
            
            # If any links found, update statistics
            if link_matches:
                crawl_state['signposting_stats']['found'] += 1
        
        # If no links were found in headers, try extracting them from HTML content
        if not links:
            try:
                response = requests.get(url, timeout=10, headers=headers) # Make a full GET request to retrieve the HTML content
                response.raise_for_status() # Raise exception for HTTP errors
                soup = BeautifulSoup(response.text, 'html.parser') # Parse the HTML content
                
                # Look for <link> elements with rel and href attributes (standard HTML links)
                link_elements = soup.find_all('link', attrs={'rel': True, 'href': True})
                for link in link_elements:
                    # Handle rel attribute which might be a list or string depending on parser
                    rel_attr = link.get('rel')
                    rel = rel_attr[0] if isinstance(rel_attr, list) else rel_attr
                    href = link.get('href')
                    links[rel] = href
                    logger.info(f"Found signposting in HTML: {rel} -> {href}")
                
                # Check for a href links with rel attributes (common alternative)
                a_links = soup.find_all('a', attrs={'rel': True, 'href': True})
                for link in a_links:
                    rel_attr = link.get('rel')
                    rel = rel_attr[0] if isinstance(rel_attr, list) else rel_attr
                    href = link.get('href')
                    links[rel] = href
                    logger.info(f"Found link relation in HTML anchor: {rel} -> {href}")
                
                # Also check for link alternates that might point to RDF resources
                alt_links = soup.find_all('link', attrs={'rel': 'alternate', 'type': True, 'href': True})
                for link in alt_links:
                    mime_type = link.get('type', '').lower()
                    # Check if the MIME type indicates an RDF format
                    if any(rdf_type in mime_type for rdf_type in ['rdf', 'turtle', 'n-triples', 'json-ld']):
                        href = link.get('href')
                        links['alternate'] = href
                        logger.info(f"Found alternate link to RDF: {mime_type} -> {href}")
                
                if link_elements or a_links or alt_links:
                    crawl_state['signposting_stats']['found'] += 1
            except Exception as html_e:
                logger.error(f"Error fetching HTML from {url}: {str(html_e)}")
    
    except Exception as e:
        logger.error(f"Error fetching signposting links from {url}: {str(e)}")
    
    return links

def fallback_discovery(url):
    """
    Fallback method when no signposting is available.
    Tries to discover RDF data using common patterns.

    This function uses heuristics to find potential RDF resources related to a URL
    when standard signposting mechanisms aren't present. It tries common URL patterns,
    extensions, and examines the HTML for embedded structured data.

    """
    potential_links = {}
    
    try:
        # Parse the URL to extract components for building variations
        parsed_url = urlparse(url)
        base_url = f"{parsed_url.scheme}://{parsed_url.netloc}"
        path_base = parsed_url.path
        
        # Get all supported RDF formats with MIME types from rdflib
        supported_mime_types = []
        try:
            supported_mime_types = [format for format in (plugin.name for plugin in rdflib.plugin.plugins(kind=rdflib.plugin.Parser)) if "/" in format]
        except:
             # Fallback to known formats if plugin enumeration fails
            supported_mime_types = [
                'application/rdf+xml', 'text/n3', 'text/turtle', 'application/n-triples', 
                'application/ld+json', 'application/n-quads', 'application/trix', 'application/trig'
            ]
        
        # Check for common RDF endpoints
        common_paths = [
            "/data",
            "/metadata",
            "/rdf",
            "/sparql",
            ".rdf",
            ".ttl",
            ".n3",
            ".nt",
            ".jsonld",
            ".json-ld",
            ".nq",
            ".trig",
            ".trix",
            "/void",
            "/lod",
            "/linked-data",
            "/about",
            ".well-known/void"
        ]
        
        path_variations = [] # List of potential URL variations to check
        path_variations.append(url)  # Include the original URL
        
        # Append common RDF file extensions to the URL
        for ext in ['.rdf', '.ttl', '.jsonld', '.n3', '.nt']:
            path_variations.append(f"{url.rstrip('/')}{ext}")
        
        # Try with common data paths
        for path in common_paths:
            if path.startswith('.'):
                # Handle extensions: append to the base path or replace existing extension
                path_without_ext = path_base.rstrip('/')
                if '.' in path_without_ext:
                    # If the path already has an extension, replace it
                    base_without_ext = path_without_ext.rsplit('.', 1)[0]
                    path_variations.append(f"{base_url}{base_without_ext}{path}")
                else:
                    # Otherwise just append the extension
                    path_variations.append(f"{base_url}{path_without_ext}{path}")
            else:
                # Handle paths: append to current path or replace last segment
                path_variations.append(f"{url.rstrip('/')}{path}")
                
                # Try replacing the last path segment for resource-specific paths
                if path_base != '/' and path_base != '':
                    parent_path = path_base.rsplit('/', 1)[0]
                    path_variations.append(f"{base_url}{parent_path}{path}")
        
        # Add variations with query parameters
        content_negotiation_params = [
            "?format=rdf",
            "?format=turtle",
            "?format=n3",
            "?format=json-ld",
            "?format=ntriples",
            "?format=xml"
        ]

        for param in content_negotiation_params:
            path_variations.append(f"{url}{param}")
        
        # Remove duplicates from the list of variations
        path_variations = list(set(path_variations))
        logger.debug(f"Generated {len(path_variations)} path variations for {url}")
        
        # Check each variation for RDF content
        for potential_url in path_variations:
            if potential_url == url:
                continue  # Skip the original URL
                
            try:
                response = requests.head(potential_url, timeout=5, 
                                         headers={'Accept': 'application/rdf+xml, text/turtle, application/n-triples, application/ld+json'})
                
                if response.status_code == 200:
                    content_type = response.headers.get('Content-Type', '').lower()
                    content_type_base = content_type.split(';')[0].strip()  # Handle content types with parameters
                    
                    # Check if content type is one of the supported RDF types
                    if any(ct in content_type for ct in supported_mime_types + ['rdf', 'turtle', 'n3', 'json-ld']):
                        logger.info(f"Found potential RDF resource at {potential_url} with Content-Type: {content_type}")
                        potential_links['alternate'] = potential_url
                        break
            except Exception as e:
                logger.debug(f"Error checking {potential_url}: {str(e)}")
                continue
        
        # If no external RDF source found, check for embedded structured data
        if not potential_links:
            try:
                response = requests.get(url, timeout=10)
                soup = BeautifulSoup(response.text, 'html.parser')
                
                # Check for JSON-LD embedded in script tags
                jsonld_scripts = soup.find_all('script', attrs={'type': 'application/ld+json'})
                if jsonld_scripts:
                    # Append fragment to original URL to indicate embedded JSON-LD
                    potential_links['describedby'] = f"{url}#jsonld"
                    logger.info(f"Found embedded JSON-LD in {url}")
                    
                # Check for RDFa attributes in HTML
                has_rdfa = False
                if soup.html:
                    # Look for RDFa namespace declarations in the HTML tag
                    if 'prefix' in soup.html.attrs or any(attr.startswith('xmlns:') for attr in soup.html.attrs):
                        has_rdfa = True

                    # Look for common RDFa attributes in any elements
                    rdfa_attrs = soup.find_all(attrs={'property': True}) or soup.find_all(attrs={'typeof': True})
                    if rdfa_attrs:
                        has_rdfa = True
                
                if has_rdfa:
                    potential_links['describedby'] = f"{url}#rdfa"
                    logger.info(f"Found RDFa in {url}")
                
                # Look for Microdata attributes (schema.org) - if available 
                microdata_elems = soup.find_all(attrs={'itemtype': True})
                if microdata_elems:
                    for elem in microdata_elems:
                        if 'schema.org' in elem.get('itemtype', ''):
                            potential_links['describedby'] = f"{url}#microdata"
                            logger.info(f"Found Microdata/schema.org in {url}")
                            break
                
                # Look for alternate links in HTML that point to RDF resources
                for link in soup.find_all('link', attrs={'rel': 'alternate', 'href': True}):
                    href = link.get('href')
                    mime_type = link.get('type', '').lower()
                    
                    if any(rdf_type in mime_type for rdf_type in ['rdf', 'turtle', 'n-triples', 'json-ld']):
                        # Resolve relative URLs to absolute URLs
                        if not href.startswith(('http://', 'https://')):
                            if href.startswith('/'):
                                href = f"{base_url}{href}"
                            else:
                                href = f"{url.rstrip('/')}/{href}"
                        
                        potential_links['alternate'] = href
                        logger.info(f"Found alternate link to RDF: {href}")
                        break
            except Exception as html_e:
                logger.error(f"Error scraping HTML from {url}: {str(html_e)}")
        
        if potential_links:  # Update statistics
            crawl_state['signposting_stats']['fallback_used'] += 1
            
    except Exception as e:
        logger.error(f"Error in fallback discovery for {url}: {str(e)}")
    
    return potential_links


def calculate_relevance(resource_url, resource_data=None):
    """
    Calculate relevance score for a resource.
    Higher score means more relevant to the current crawl focus.
    Scores range from 0.0 to 1.0, with higher scores indicating more 
    relevant resources that should be prioritised for processing.
    """
    base_score = 0.3  # Start with a base score

    # Check content type via HEAD request to identify RDF formats
    try:
        head_resp = requests.head(resource_url, timeout=5, allow_redirects=True)
        content_type = head_resp.headers.get('Content-Type', '').lower()
        # Boost score if content type indicates RDF data
        if any(ct in content_type for ct in ['rdf', 'turtle', 'n3', 'json-ld', 'xml', 'n-triples', 'n-quads', 'trig', 'trix']):
            base_score += 0.2
    except:
        pass
    
    if resource_url in crawl_state['visited_urls']: # to avoid cycles, if already seen
        return 0.0
    
    try:
        parsed_url = urlparse(resource_url) # Parse URL to enable path-based analysis
        
        # Check if URL contains keywords commonly associated with RDF data
        rdf_indicators = [
            'data', 'metadata', 'rdf', 'resource', 'catalog', 'dataset', 
            'fair', 'sparql', 'ontology', 'vocab', 'linked', 'lod', 
            'semantic', 'graph', 'json-ld', 'jsonld', 'turtle', 'n3',
            'void', 'concept', 'term', 'knowledge'
        ]
        
        for indicator in rdf_indicators: # Boost score if URL contains RDF-related keywords
            if indicator in parsed_url.path.lower():
                base_score += 0.1
                break  # Only add this bonus once, even if multiple keywords match
        
        # Boost score significantly if URL has a known RDF file extension
        if resource_url.endswith(('.rdf', '.ttl', '.n3', '.jsonld', '.nt', '.nq', '.trig', '.trix')):
            base_score += 0.3
        
        if resource_data and isinstance(resource_data, Graph):
            # More triples means more useful information
            # Award progressively higher scores based on triple count
            triple_count = len(resource_data)
            if triple_count > 100:
                base_score += 0.2
            elif triple_count > 50:
                base_score += 0.1
            elif triple_count > 10:
                base_score += 0.05
            
            # Check for important vocabularies == high-quality data
            important_vocabs = set([
                'http://schema.org/', 
                'http://purl.org/', 
                'http://www.w3.org/ns/dcat#',
                'http://purl.org/dc/terms/', 
                'http://www.w3.org/2004/02/skos/core#',
                'http://xmlns.com/foaf/0.1/',
                'http://www.w3.org/ns/prov#',
                'http://rdfs.org/ns/void#',
                'http://www.w3.org/2002/07/owl#',
                'http://www.w3.org/ns/oa#'
            ])
            
            vocab_count = 0 # Count unique vocabularies used in the graph
            found_vocabs = set()
            
            # Check each predicate against known vocabulary namespaces
            for _, p, _ in resource_data:
                str_p = str(p)
                for vocab in important_vocabs:
                    if vocab in str_p and vocab not in found_vocabs:
                        vocab_count += 1
                        found_vocabs.add(vocab)
            
            # Award higher scores for diverse vocabulary usage
            if vocab_count >= 3:
                base_score += 0.3
            elif vocab_count >= 1:
                base_score += 0.1 * vocab_count
            
            # Check for specific useful classes or properties
            useful_terms = set([
                str(dcat.Dataset),
                str(schema.Dataset),
                str(void.Dataset),
                str(schema.Person),
                str(FOAF.Person),
                str(schema.Organization),
                str(FOAF.Organization),
                str(schema.ScholarlyArticle),
                str(schema.CreativeWork),
                str(DC.title),
                str(schema.name),
                str(schema.description),
                str(DC.description),
                str(DC.creator),
                str(schema.creator),
                str(schema.author)
            ])
            
            # Count occurrences of useful terms
            useful_count = 0
            for s, p, o in resource_data:
                if str(p) in useful_terms or str(o) in useful_terms:
                    useful_count += 1
                    if useful_count >= 3:
                        base_score += 0.2
                        break
                    
    except Exception as e:
        logger.error(f"Error calculating relevance for {resource_url}: {str(e)}")
        # Return a moderate score on error to avoid completely skipping potentially useful resources
        return 0.4
    
    # Ensure score is between 0 and 1
    return min(1.0, max(0.0, base_score))


def fetch_and_parse_rdf(url):
    """
    Fetch RDF data from a URL and parse it.
    Returns a tuple of (RDF graph, error message if any, format used, content type).

    This function attempts to retrieve RDF data from a URL using various methods,
    including direct parsing, content negotiation, and special handling for embedded
    structured data. It tries multiple RDF formats and follows a fallback chain to
    maximise the chance of successfully extracting triples.

    """
    # Get all supported RDF formats from rdflib
    supported_formats = []
    try:
        # Extract formats with MIME types (those containing '/')
        supported_formats = [format for format in (plugin.name for plugin in rdflib.plugin.plugins(kind=rdflib.plugin.Parser)) if "/" in format]
        logger.info(f"Supported rdflib formats with MIME types: {supported_formats}")
    except Exception as e:
        logger.warning(f"Could not get supported RDF formats: {str(e)}")
        # Fallback to common known formats if plugin system query fails
        supported_formats = [
            'application/rdf+xml', 'text/n3', 'text/turtle', 'application/n-triples', 
            'application/ld+json', 'application/n-quads', 'application/trix', 'application/trig'
        ]
    
    # Map file extensions to rdflib format names
    ext_to_format = {
        '.rdf': 'xml',
        '.ttl': 'turtle',
        '.n3': 'n3',
        '.jsonld': 'json-ld',
        '.json': 'json-ld',  # Try JSON-LD for regular JSON too
        '.nt': 'nt',
        '.nq': 'nquads',
        '.trig': 'trig',
        '.trix': 'trix'
    }
    
    # Map MIME types to rdflib format names
    mime_to_format = {
        'application/rdf+xml': 'xml',
        'text/turtle': 'turtle',
        'text/n3': 'n3',
        'application/n-triples': 'nt',
        'application/ld+json': 'json-ld',
        'application/json': 'json-ld', 
        'application/n-quads': 'nquads',
        'application/trix': 'trix',
        'application/trig': 'trig'
    }
    
    # Early exit for non-RDF file extensions and content types
    if not any(url.endswith(ext) for ext in ext_to_format.keys()):
        try:
            head_resp = requests.head(url, timeout=5, allow_redirects=True)
            content_type = head_resp.headers.get('Content-Type', '').lower()
            content_type_base = content_type.split(';')[0].strip()  # Handle content types with parameters
            
            # Skip parsing if content type clearly indicates non-RDF content
            if not any(ct in content_type for ct in supported_formats + ['application/xml', 'text/xml']):
                logger.info(f"Skipping non-RDF resource based on Content-Type: {content_type}")
                return Graph(), "Non-RDF content type", None, None
        except Exception as e:
            logger.warning(f"Error checking content type: {str(e)}")
             # Continue anyway since content type check is just an optimisation
    
     # Initialise empty graph and error tracking
    g = Graph()
    error_msg = None
    
    try:
        logger.info(f"Attempting to fetch and parse RDF from {url}")
        
        # SPECIAL CASE HANDLING: process URLs with fragments indicating embedded structured data
        if url.endswith('#jsonld'):
            base_url = url[:-7]  # Remove #jsonld fragment
            logger.info(f"Processing as embedded JSON-LD from {base_url}")
            try:
                response = requests.get(base_url, timeout=10)  # Fetch the HTML page
                response.raise_for_status()  # Raise exception for HTTP errors
                soup = BeautifulSoup(response.text, 'html.parser')
                jsonld_scripts = soup.find_all('script', attrs={'type': 'application/ld+json'})
                
                if not jsonld_scripts:
                    error_msg = f"No JSON-LD scripts found in {base_url}"
                    logger.warning(error_msg)
                    return g, error_msg, None, None
                
                # Try to parse each JSON-LD script found
                for script in jsonld_scripts:
                    try:
                        if script.string and script.string.strip():
                            g.parse(data=script.string, format='json-ld')
                            logger.info(f"Successfully parsed JSON-LD script from {base_url}")
                        else:
                            logger.warning(f"Empty JSON-LD script found in {base_url}")
                    except Exception as e:
                        error_msg = f"Error parsing JSON-LD from {url}: {str(e)}"
                        logger.error(error_msg)
                
                # Return results if we found any triples
                if len(g) > 0:
                    return g, None, 'json-ld', 'application/ld+json'
                else:
                    error_msg = f"No valid triples found in JSON-LD from {base_url}"
                    return g, error_msg, None, None
            except requests.exceptions.RequestException as e:
                error_msg = f"Error fetching {base_url}: {str(e)}"
                logger.error(error_msg)
                return g, error_msg, None, None

        # Handle embedded RDFa    
        elif url.endswith('#rdfa'):
            base_url = url[:-5]  # Remove #rdfa fragment
            logger.info(f"Processing as RDFa from {base_url}")
            try:
                response = requests.get(base_url, timeout=10)
                response.raise_for_status()
                try:
                    g.parse(data=response.text, format='rdfa', publicID=base_url)
                    logger.info(f"Successfully parsed RDFa from {base_url}")
                    return g, None, 'rdfa', 'text/html'
                except Exception as e:
                    error_msg = f"Error parsing RDFa from {url}: {str(e)}"
                    logger.error(error_msg)
                    return g, error_msg, None, None
            except requests.exceptions.RequestException as e:
                error_msg = f"Error fetching {base_url}: {str(e)}"
                logger.error(error_msg)
                return g, error_msg, None, None
            
        # REGULAR CASE: Process standard RDF resources
        # Determine format from URL extension
        format_hint = None
        for ext, fmt in ext_to_format.items():
            if url.endswith(ext):
                format_hint = fmt
                break
        
        logger.info(f"Determined format hint from URL: {format_hint} for {url}")
        
        # Try to fetch and parse the RDF using the format hint
        try:
            g.parse(url, format=format_hint)
            logger.info(f"Successfully parsed RDF directly from {url} with format {format_hint}")
            return g, None, format_hint, None
        except Exception as direct_e:
            logger.warning(f"Direct parsing failed for {url}: {str(direct_e)}")
            
            # Try fetching the content first, then try multiple formats
            try:
                response = requests.get(url, timeout=10)
                response.raise_for_status()
                logger.info(f"Fetched content from {url}: Status={response.status_code}, Content-Type={response.headers.get('Content-Type')}")
                
                # Try to determine format from content type header 
                content_type = response.headers.get('Content-Type', '').lower()
                content_type_base = content_type.split(';')[0].strip()  # Handle content types with parameters
                content_format = mime_to_format.get(content_type_base)
                
                logger.info(f"Determined format from Content-Type: {content_format} for {content_type_base}")
                
                formats_to_try = [] # Create prioritised list of formats to try
                if content_format:
                    formats_to_try.append(content_format)# First try format based on content type if available
                if format_hint:
                    formats_to_try.append(format_hint)# Then try format based on URL extension if available
                
                # Add all common formats as fallbacks
                for fmt in ['turtle', 'xml', 'json-ld', 'n3', 'nt', 'nquads', 'trig', 'trix', 'hext']:
                    if fmt not in formats_to_try:
                        formats_to_try.append(fmt)
                
                logger.info(f"Will try parsing with formats: {formats_to_try}")
                
                # Try each format in priority order
                for fmt in formats_to_try:
                    try:
                        g.parse(data=response.text, format=fmt)
                        logger.info(f"Successfully parsed content from {url} with format {fmt}")
                        return g, None, fmt, content_type
                    except Exception as parse_e:
                        logger.warning(f"Parsing with format {fmt} failed: {str(parse_e)}")
                
                # If regular RDF parsing failed, check for structured data in HTML
                # Try to detect RDFa in HTML content
                if '<html' in response.text.lower():
                    try:
                        g.parse(data=response.text, format='rdfa', publicID=url)
                        logger.info(f"Successfully parsed as RDFa from HTML content at {url}")
                        return g, None, 'rdfa', 'text/html'
                    except Exception as rdfa_e:
                        logger.warning(f"RDFa parsing failed: {str(rdfa_e)}")
                
                # Check for Microdata in HTML content (requires optional extension)
                if '<html' in response.text.lower():
                    try:
                        from rdflib_microdata import MicrodataParser
                        g.parse(data=response.text, format='microdata', publicID=url)
                        logger.info(f"Successfully parsed as Microdata from HTML content at {url}")
                        return g, None, 'microdata', 'text/html'
                    except ImportError:
                        logger.warning("rdflib_microdata not available, skipping Microdata parsing")
                    except Exception as microdata_e:
                        logger.warning(f"Microdata parsing failed: {str(microdata_e)}")
                
                # If all parsing attempts failed, return an empty graph with error
                error_msg = "Failed to parse content with any known RDF format"
                logger.error(error_msg)
                return g, error_msg, None, content_type
                
            except requests.exceptions.RequestException as req_e:
                error_msg = f"Error fetching content from {url}: {str(req_e)}"
                logger.error(error_msg)
                return g, error_msg, None, None
    
    except Exception as outer_e:
        error_msg = f"Unexpected error in fetch_and_parse_rdf for {url}: {str(outer_e)}"
        logger.error(error_msg)
    
    logger.warning(f"All parsing methods failed for {url}, returning empty graph")
    if not error_msg:
        error_msg = "All parsing methods failed"
    return g, error_msg, None, None


def store_in_fuseki(graph_data, named_graph=None, max_retries=3):
    """
    Store the RDF graph in Fuseki with retry mechanism.
    """
    # Skip empty graphs
    if len(graph_data) == 0:
        logger.warning("Attempted to store empty graph in Fuseki, skipping")
        return False
 
    if not named_graph:
        # Use a timestamp-based identifier instead of random UUID
        timestamp = datetime.datetime.now().strftime('%Y%m%d%H%M%S')
        # Generate a more meaningful named graph identifier based on the crawl ID
        named_graph = f"http://crawl.data/{crawl_state['crawl_id']}/graph/{timestamp}"
    
    retry_count = 0 # Implement retry logic
    while retry_count < max_retries:
        try:
            # Set up SPARQL endpoint for updates
            sparql = SPARQLWrapper(f"{app.config['FUSEKI_ENDPOINT']}/{app.config['FUSEKI_DATASET']}/update")
            sparql.setMethod('POST')
            sparql.setRequestMethod('POST')  # Explicitly set request method
            sparql.setReturnFormat(JSON)    # Set a return format
            
            # First check if the graph already exists
            check_sparql = SPARQLWrapper(f"{app.config['FUSEKI_ENDPOINT']}/{app.config['FUSEKI_DATASET']}/query")
            check_sparql.setReturnFormat(JSON)
            check_sparql.setQuery(f"""
            ASK WHERE {{ 
                GRAPH <{named_graph}> {{ ?s ?p ?o }} 
            }}
            """)
            
            result = check_sparql.query().convert()
            if result.get('boolean', False):
                # The graph exists - use merge approach
                logger.info(f"Graph {named_graph} already exists, merging new data")
                
                # Serialise to N-Triples format for consistent SPARQL update
                try:
                    ntriples_data = graph_data.serialize(format='nt').decode('utf-8') if isinstance(graph_data.serialize(format='nt'), bytes) else graph_data.serialize(format='nt')
                except Exception as ser_e:
                    logger.error(f"Error serializing graph for merge: {str(ser_e)}")
                    # Try turtle as fallback serialisation format
                    ntriples_data = graph_data.serialize(format='turtle').decode('utf-8') if isinstance(graph_data.serialize(format='turtle'), bytes) else graph_data.serialize(format='turtle')
                
                # For large graphs, split into chunks to avoid SPARQL endpoint limits
                if len(ntriples_data) > 100000:  # If more than ~100KB
                    lines = ntriples_data.split('\n')
                    chunk_size = 1000  # Process 1000 triples at a time
                    chunks = [lines[i:i+chunk_size] for i in range(0, len(lines), chunk_size)]
                    
                    for i, chunk in enumerate(chunks): # Process each chunk separately
                        chunk_data = '\n'.join(chunk)
                        # Skip empty chunks
                        if not chunk_data.strip():
                            continue
                            
                        # Construct query for this chunk    
                        update_query = f"""
                        INSERT DATA {{ 
                            GRAPH <{named_graph}> {{ 
                                {chunk_data}
                            }}
                        }}
                        """
                        
                        sparql.setQuery(update_query)
                        try:
                            sparql.query()
                            logger.info(f"Stored chunk {i+1}/{len(chunks)} in graph {named_graph}")
                        except Exception as chunk_e:
                            logger.error(f"Error storing chunk {i+1}: {str(chunk_e)}")
                else:
                    update_query = f"""
                    INSERT DATA {{ 
                        GRAPH <{named_graph}> {{ 
                            {ntriples_data}
                        }}
                    }}
                    """
                    
                    sparql.setQuery(update_query)
                    sparql.query()
            else:
                # The graph doesn't exist, create it with the data
                try:
                    ntriples_data = graph_data.serialize(format='nt').decode('utf-8') if isinstance(graph_data.serialize(format='nt'), bytes) else graph_data.serialize(format='nt')
                except Exception as ser_e:
                    logger.error(f"Error serializing graph: {str(ser_e)}")
                    # Fall back to turtle if N-Triples fails
                    turtle_data = graph_data.serialize(format='turtle').decode('utf-8') if isinstance(graph_data.serialize(format='turtle'), bytes) else graph_data.serialize(format='turtle')
                    # Escape triple quotes in the turtle data to avoid SPARQL syntax errors
                    turtle_data = turtle_data.replace('"""', '\\"\\"\\"')
                    
                    # Construct SPARQL update query with Turtle data
                    update_query = f"""
                    INSERT DATA {{ 
                        GRAPH <{named_graph}> {{ 
                            {turtle_data}
                        }}
                    }}
                    """
                else:
                    # Use N-Triples data in the query
                    update_query = f"""
                    INSERT DATA {{ 
                        GRAPH <{named_graph}> {{ 
                            {ntriples_data}
                        }}
                    }}
                    """
                
                sparql.setQuery(update_query)  # Execute the update query
                sparql.query()
            # Success! Log and return
            logger.info(f"Stored {len(graph_data)} triples in Fuseki named graph: {named_graph}")
            return True
            
        except Exception as e: # Handle errors with retry logic
            retry_count += 1
            logger.warning(f"Error storing data in Fuseki (attempt {retry_count}/{max_retries}): {str(e)}")
            if retry_count < max_retries:
                time.sleep(1)  # Wait before retrying
            else:
                logger.error(f"Failed to store data in Fuseki after {max_retries} attempts")
                return False
                
    return False


def record_format_statistics(url, content_type, format_used, rel_type=None):
    """
    Record statistics about RDF formats and content types found.
    """
    # Track MIME types
    if content_type:
        content_type_base = content_type.split(';')[0].strip().lower()
        if content_type_base in crawl_state['signposting_stats']['mime_types']:
            crawl_state['signposting_stats']['mime_types'][content_type_base] += 1
        else:
            crawl_state['signposting_stats']['mime_types'][content_type_base] = 1
    
    # Track relation types - count occurrences of each type
    if rel_type:
        if rel_type in crawl_state['signposting_stats']['rel_types']:
            crawl_state['signposting_stats']['rel_types'][rel_type] += 1
        else:
            crawl_state['signposting_stats']['rel_types'][rel_type] = 1
    
    # Add domain to visited domains
    try:
        domain = urlparse(url).netloc
        crawl_state['provenance']['domains_visited'].add(domain)
    except:
        pass

def record_provenance(resource_url, data_source_type, triple_count, format_used=None, content_type=None):
    """
    Record provenance information about crawled resources.

    This function stores detailed metadata about each resource processed during
    the crawl, including its source, format, and the number of triples extracted.
    It updates both the in-memory provenance record and creates formal RDF provenance
    statements using PROV-O ontology, which are stored in Fuseki.

    """

    provenance_id = resource_url.replace('://', '/').replace('/', '_').replace(':', '_')
    timestamp = datetime.datetime.now().isoformat()

    # Initialise the resources list if it doesn't exist yet
    if 'resources' not in crawl_state['provenance']:
        crawl_state['provenance']['resources'] = []
    
    
    # Extract the relation type from data_source_type if it's a signposting link
    rel_type = None
    if data_source_type.startswith('signposting:'):
        rel_type = data_source_type.split(':')[1]
    
    # Update format statistics counters
    record_format_statistics(resource_url, content_type, format_used, rel_type)
    
    # Create a resource information dictionary with metadata
    resource_info = {
        'id': provenance_id,
        'url': resource_url,
        'source_type': data_source_type,
        'triple_count': triple_count,
        'timestamp': timestamp,
        'crawl_depth': crawl_state['current_depth']
    }
    
    # Add format information if available
    if format_used:
        resource_info['format'] = format_used
    if content_type:
        resource_info['content_type'] = content_type
    
    crawl_state['provenance']['resources'].append(resource_info) # Append to the list of resources in the provenance record
    
    # Update overall stats
    crawl_state['provenance']['resources_visited'] += 1
    crawl_state['provenance']['triples_collected'] += triple_count
    
    # Create formal RDF provenance using PROV-O ontology
    prov_g = Graph()
    
    # Bind namespaces
    prov_g.bind("prov", prov)
    prov_g.bind("dc", DC)
    prov_g.bind("schema", schema)
    
    # Create URIs - Use actual URLs instead of placeholder URIs
    crawl_uri = URIRef(f"http://crawl.data/{crawl_state['crawl_id']}")
    resource_uri = URIRef(resource_url)  # Use the actual resource URL
    
    # Add provenance triples
    prov_g.add((resource_uri, RDF.type, prov.Entity))
    prov_g.add((resource_uri, prov.wasGeneratedBy, crawl_uri))
    prov_g.add((resource_uri, DC.source, URIRef(resource_url)))
    prov_g.add((resource_uri, DCTERMS.created, Literal(timestamp, datatype=XSD.dateTime)))
    prov_g.add((resource_uri, prov.value, Literal(triple_count)))
    prov_g.add((resource_uri, prov.type, Literal(data_source_type)))
    
    # Add format information using appropriate predicates
    if format_used:
        prov_g.add((resource_uri, DC.format, Literal(format_used)))
    if content_type:
        prov_g.add((resource_uri, schema.encodingFormat, Literal(content_type)))
    
    # Store the provenance information in Fuseki under a dedicated graph
    store_in_fuseki(prov_g, f"http://crawl.data/{crawl_state['crawl_id']}/provenance")


def crawl_resource(url, depth=0):
    """
    Crawl a resource, follow signposting, and update the knowledge graph.
    Returns a list of discovered URLs for further crawling.
    """
    # Normalise URL first
    parsed_url = urlparse(url)
    if not parsed_url.scheme:
        url = f"http://{url}"  # Default to http if no scheme is specified
    
    # URL-decode the URL to handle encoded characters
    url = unquote(url)
    # Skip already visited URLs to prevent cycles
    if url in crawl_state['visited_urls']:
        logger.debug(f"Skipping already visited URL: {url}")
        return []
    
    logger.info(f"Crawling resource at depth {depth}: {url}")
    crawl_state['visited_urls'].add(url)
    crawl_state['current_depth'] = depth
    
    # Try to directly parse the URL as RDF
    direct_graph, direct_error, format_used, content_type = fetch_and_parse_rdf(url)
    if len(direct_graph) > 0:
        # RDF data found directly at this URL
        triple_count = len(direct_graph)
        logger.info(f"Found {triple_count} triples directly at {url} using format {format_used}")
        
        # Calculate relevance
        relevance = calculate_relevance(url, direct_graph)
        crawl_state['resource_scores'][url] = relevance
        
        # Store in Fuseki if it meets the relevance threshold
        if relevance >= app.config['RELEVANCE_THRESHOLD']:
            try:
                graph_name = url  # Use the resource URL directly as graph name
                success = store_in_fuseki(direct_graph, graph_name)
                if success:
                    record_provenance(url, "direct_rdf", triple_count, format_used, content_type)
                    logger.info(f"Stored direct RDF from {url} into {graph_name} in Fuseki")
                else:
                    logger.warning(f"Failed to store direct RDF from {url} in Fuseki")
            except Exception as e:
                logger.error(f"Exception storing direct RDF from {url} in Fuseki: {str(e)}")
    
    # Find links to related resources using standard signposting mechanisms
    links = get_signposting_links(url)
    
    # If no signposting found, try fallback methods
    if not links:
        logger.info(f"No signposting found at {url}, trying fallback discovery")
        links = fallback_discovery(url)
    
    logger.info(f"Found links at {url}: {links}")
    
    discovered_urls = [] # List to collect URLs that should be crawled next
    
    # Check if this is a known repository or data catalog
    is_repository = any(repo in url.lower() for repo in [
        'zenodo.org', 'figshare.com', 'datadryad.org', 'dataverse', 'ands.org.au',
        'doi.org', 'datacite.org', 'pangaea.de', 'ncbi.nlm.nih.gov', 'orcid.org',
        'github.com', 'gitlab.com', 'bitbucket.org', 'sourceforge.net'
    ])
    
    # Process each linked resource based on relation type
    for rel, target_url in links.items():
        # Handle relative URLs
        try:
            if not target_url.startswith(('http://', 'https://')):
                parsed_url = urlparse(url)
                base_url = f"{parsed_url.scheme}://{parsed_url.netloc}"
                if target_url.startswith('/'):
                    target_url = f"{base_url}{target_url}"
                else:
                    last_slash = url.rfind('/')
                    if last_slash > 8:  # After http:// or https://
                        target_url = f"{url[:last_slash+1]}{target_url}"
                    else:
                        target_url = f"{url.rstrip('/')}/{target_url}"
                logger.info(f"Resolved relative URL to: {target_url}")
        except Exception as e:
            logger.error(f"Error resolving relative URL {target_url} from {url}: {str(e)}")
            continue
        
        # URL-decode the resolved URL
        target_url = unquote(target_url)
        
        # Skip if already visited
        if target_url in crawl_state['visited_urls']:
            logger.debug(f"Skipping already visited linked URL: {target_url}")
            continue
        
        # Identify priority relation types
        priority_rel = rel in [
            'describedby', 'describes', 'item', 'collection', 'cite-as', 
            'author', 'license', 'type', 'profile', 'alternate'
        ]
        
        # Add relevance boost for resources from known repositories
        repository_boost = 0.2 if is_repository else 0.0
        
        # Try to fetch and parse RDF from linked resource
        try:
            rdf_graph, error_msg, format_used, content_type = fetch_and_parse_rdf(target_url)
            triple_count = len(rdf_graph)
            
            if triple_count > 0: # RDF found at linked resource
                logger.info(f"Found {triple_count} triples at linked resource {target_url} using format {format_used}")
                
                # Calculate relevance with priority boost for important links
                relevance = calculate_relevance(target_url, rdf_graph)
                if priority_rel:
                    relevance += 0.1  # Boost for priority relation
                relevance += repository_boost
                
                relevance = min(1.0, relevance)  # Cap at 1.0
                crawl_state['resource_scores'][target_url] = relevance
                
                # If relevant enough, store in Fuseki
                if relevance >= app.config['RELEVANCE_THRESHOLD']:
                    try:
                        success = store_in_fuseki(rdf_graph)
                        if success:
                            record_provenance(target_url, f"signposting:{rel}", triple_count, format_used, content_type)
                            discovered_urls.append(target_url)
                            logger.info(f"Stored RDF from {target_url} in Fuseki")
                        else:
                            logger.warning(f"Failed to store RDF from {target_url} in Fuseki")
                    except Exception as e:
                        logger.error(f"Exception storing RDF from {target_url} in Fuseki: {str(e)}")
                
                logger.info(f"Processed RDF from {target_url}: {triple_count} triples, relevance {relevance:.2f}, format {format_used}")
            else:
                logger.warning(f"No triples found at linked resource {target_url}. Error: {error_msg}")
                # Still add to discovered URLs if it's a relation worth following
                if priority_rel:
                    discovered_urls.append(target_url)
                    logger.info(f"Following {rel} relation to {target_url} despite no RDF found")
        except Exception as e:
            logger.error(f"Error processing RDF from {target_url}: {str(e)}")
            # Still add to discovered URLs for certain relation types
            if priority_rel:
                discovered_urls.append(target_url)
                logger.info(f"Following {rel} relation to {target_url} despite error")
    # Return list of newly discovered URLs for the crawler to process next
    return discovered_urls

def should_continue_crawl():
    """
    Determine if the crawl should continue based on various heuristics.
    """
    # Check if we've reached max depth configured in the application
    if crawl_state['current_depth'] >= app.config['MAX_CRAWL_DEPTH']:
        logger.info(f"Stopping crawl: reached max depth {app.config['MAX_CRAWL_DEPTH']}")
        return False
    
    # Check if we've spent too much time
    elapsed_time = (datetime.datetime.now() - crawl_state['start_time']).total_seconds()
    if elapsed_time > app.config['CRAWL_TIMEOUT']:
        logger.info(f"Stopping crawl: timeout after {elapsed_time} seconds")
        return False
    
    # Check if we've found enough data
    if crawl_state['provenance'].get('triples_collected', 0) > app.config.get('MAX_TRIPLES', 10000):
        logger.info(f"Stopping crawl: collected sufficient triples (>{app.config.get('MAX_TRIPLES', 10000)})")
        return False
    
    # Check if we've visited enough resources - prevents excessive crawling
    if len(crawl_state['visited_urls']) > app.config.get('MAX_RESOURCES', 500):
        logger.info(f"Stopping crawl: visited maximum number of resources ({len(crawl_state['visited_urls'])})")
        return False
    
    # Check if we're making progress - stops if no new triples are being found
    if crawl_state.get('last_triples_count') == crawl_state['provenance'].get('triples_collected', 0) and \
       crawl_state.get('no_progress_count', 0) > 3:
        logger.info("Stopping crawl: no new triples collected in last few iterations")
        return False
    
    # Update progress tracking counters
    crawl_state['last_triples_count'] = crawl_state['provenance'].get('triples_collected', 0)
    if crawl_state.get('prev_triples_count') == crawl_state['last_triples_count']:
        # Increment counter if no progress since last check
        crawl_state['no_progress_count'] = crawl_state.get('no_progress_count', 0) + 1
    else:
        # Reset counter if progress was made    
        crawl_state['no_progress_count'] = 0
    # Save current count for next comparison
    crawl_state['prev_triples_count'] = crawl_state['last_triples_count']
    
    # If none of the stopping criteria were met, continue crawling
    return True

def select_next_resources(candidate_urls):
    """
    Select which resources to crawl next based on relevance scores and domain diversity.

    This function implements a selection strategy that balances exploring the most 
    relevant resources while ensuring diversity across domains. This prevents the 
    crawler from getting stuck in a single domain (like a large repository) and 
    promotes broader coverage of the linked data landscape.

    """
    # Calculate scores for new candidates
    for url in candidate_urls:
        if url not in crawl_state['resource_scores']:
            crawl_state['resource_scores'][url] = calculate_relevance(url)
    
    # Group URLs by domain to promote domain-based diversity
    domains = {}
    for url in candidate_urls:
        if url not in crawl_state['visited_urls']:
            parsed = urlparse(url)
            domain = parsed.netloc
            # Create domain entry if it doesn't exist
            if domain not in domains:
                domains[domain] = []
            domains[domain].append(url)
    
    # Calculate how many URLs to take from each domain
    selected_urls = []
    max_per_domain = max(1, app.config['MAX_RESOURCES_PER_LEVEL'] // max(1, len(domains)))
    
    # Sort domains by their highest scoring URL
    domain_max_scores = {}
    for domain, urls in domains.items():
        domain_max_scores[domain] = max([crawl_state['resource_scores'].get(url, 0) for url in urls])
    # Sort domains by their max score, highest first
    sorted_domains = sorted(domains.keys(), key=lambda d: domain_max_scores[d], reverse=True)
    
    # Select top URLs from each domain
    for domain in sorted_domains:
        domain_urls = domains[domain]
        # Sort URLs within this domain by score
        domain_urls.sort(key=lambda url: crawl_state['resource_scores'].get(url, 0), reverse=True)
        # Select top N URLs from this domain
        selected_urls.extend(domain_urls[:max_per_domain])
        if len(selected_urls) >= app.config['MAX_RESOURCES_PER_LEVEL']:
            break
    
    # If we haven't filled our quota, add more URLs from high-scoring domains
    if len(selected_urls) < app.config['MAX_RESOURCES_PER_LEVEL']:
        remaining_urls = []
        for domain in sorted_domains:
            domain_urls = domains[domain]
            remaining = [url for url in domain_urls if url not in selected_urls]
            remaining_urls.extend(remaining)
        
        # Sort remaining by score
        remaining_urls.sort(key=lambda url: crawl_state['resource_scores'].get(url, 0), reverse=True)
        # Add until we reach the limit
        selected_urls.extend(remaining_urls[:app.config['MAX_RESOURCES_PER_LEVEL'] - len(selected_urls)])
    
    # Log selection statistics for monitoring
    domain_counts = {}
    for url in selected_urls:
        domain = urlparse(url).netloc
        domain_counts[domain] = domain_counts.get(domain, 0) + 1
    
    logger.info(f"Selected {len(selected_urls)} URLs from {len(domain_counts)} domains")
    for domain, count in domain_counts.items():
        logger.info(f"  - {domain}: {count} URLs")
    # Return selected URLs, enforcing the configured limit
    return selected_urls[:app.config['MAX_RESOURCES_PER_LEVEL']]

def export_provenance():
    """
    Export provenance information as RDF, following PROV-O and VoID standards.
    """
    prov_g = Graph()
    
    # Bind namespaces
    prov_g.bind("prov", prov)
    prov_g.bind("dc", DC)
    prov_g.bind("dcat", dcat)
    prov_g.bind("void", void)
    prov_g.bind("foaf", FOAF)
    prov_g.bind("schema", schema)
    
    # Create URI identifiers for the main provenance entities
    crawl_uri = URIRef(f"http://crawl.data/{crawl_state['crawl_id']}") # The crawl activity
    dataset_uri = URIRef(f"http://crawl.data/{crawl_state['crawl_id']}/dataset") # The resulting dataset
    agent_uri = URIRef("http://crawler.fair-signposting.org/agent/FAIRSignpostingCrawler") # The crawler agent
    
    # Add seed URLs as PROV Entities that were used by the crawl
    for i, seed_url in enumerate(crawl_state['provenance'].get('seed_urls', [])):
        seed_uri = URIRef(seed_url)  # Use the actual seed URL
        # Describe the seed URL using standard ontology terms
        prov_g.add((seed_uri, RDF.type, prov.Entity))
        prov_g.add((seed_uri, RDF.type, void.Dataset))
        prov_g.add((seed_uri, void.rootResource, URIRef(seed_url)))
        prov_g.add((seed_uri, DC.source, URIRef(seed_url)))
        # Link the seed to the crawl activity
        prov_g.add((crawl_uri, prov.used, seed_uri))
        
        # Link the dataset to its seed
        prov_g.add((dataset_uri, void.subset, seed_uri))
    
    # Add each resource that was discovered and processed
    if 'resources' in crawl_state['provenance']:
        for resource in crawl_state['provenance']['resources']:
            resource_uri = URIRef(resource['url'])  
            # Add basic metadata about the resource
            prov_g.add((resource_uri, RDF.type, prov.Entity))
            prov_g.add((resource_uri, DC.source, URIRef(resource['url'])))
            prov_g.add((resource_uri, prov.value, Literal(resource['triple_count'], datatype=XSD.integer)))
            prov_g.add((resource_uri, DCTERMS.created, Literal(resource['timestamp'], datatype=XSD.dateTime)))
            # Link to crawl provenance
            prov_g.add((resource_uri, prov.wasGeneratedBy, crawl_uri))
            prov_g.add((dataset_uri, void.subset, resource_uri))
            # Add resource type information
            prov_g.add((resource_uri, DC.type, Literal(resource['source_type'])))
            # Record the depth at which this resource was found
            prov_g.add((resource_uri, schema.position, Literal(resource['crawl_depth'], datatype=XSD.integer)))
    
    return prov_g

@app.route('/')
def index():
    # Check if Fuseki triplestore is running and accessible
    fuseki_status = "Unknown"
    try:
        response = requests.get(f"{app.config['FUSEKI_ENDPOINT']}/$/ping", timeout=3)
        if response.status_code == 200:
            fuseki_status = "Connected"
        else:
            fuseki_status = f"Error: Status {response.status_code}"
    except Exception as e:
        fuseki_status = f"Error: {str(e)}"
    
    # Get supported RDF formats
    supported_formats = []
    try:
        supported_formats = [format for format in (plugin.name for plugin in rdflib.plugin.plugins(kind=rdflib.plugin.Parser))]
    except:
        supported_formats = ["Error retrieving formats"]
    
    # Gather RDF format statistics 
    format_info = {}
    for fmt in supported_formats:
        if '/' in fmt:  # It's a MIME type
            key = 'mime_types'
        elif fmt in ['xml', 'turtle', 'n3', 'json-ld', 'nt', 'nquads', 'trig', 'hext']:
            key = 'common_formats'
        else:
            key = 'other_formats'
        
        # Add to the appropriate category
        if key not in format_info:
            format_info[key] = []
        format_info[key].append(fmt)
    
    # Check for available optional modules
    extensions = {
        'microdata': HAS_MICRODATA
    }
    
    return render_template('index.html', 
                           crawl_active=crawl_state['crawl_active'],
                           crawl_id=crawl_state.get('crawl_id'),
                           config=app.config,
                           fuseki_status=fuseki_status,
                           format_info=format_info,
                           extensions=extensions)

@app.route('/start-crawl', methods=['POST'])
def start_crawl():
    # Get the seed URL from the form submission
    seed_url = request.form.get('seed_url')
    if not seed_url:
        return jsonify({'error': 'No seed URL provided'}), 400
    
    # Handle multiple seed URLs
    seed_urls = []
    if ',' in seed_url:
        seed_urls = [url.strip() for url in seed_url.split(',') if url.strip()]
    else:
        seed_urls = [seed_url.strip()]
    
    if not seed_urls:
        return jsonify({'error': 'No valid seed URLs provided'}), 400
    
    # Update crawler configuration from form parameters
    if request.form.get('max_depth'):
        app.config['MAX_CRAWL_DEPTH'] = int(request.form.get('max_depth'))
    if request.form.get('max_resources'):
        app.config['MAX_RESOURCES_PER_LEVEL'] = int(request.form.get('max_resources'))
    if request.form.get('relevance_threshold'):
        app.config['RELEVANCE_THRESHOLD'] = float(request.form.get('relevance_threshold'))
    if request.form.get('timeout'):
        app.config['CRAWL_TIMEOUT'] = int(request.form.get('timeout'))
    if request.form.get('max_triples'):
        app.config['MAX_TRIPLES'] = int(request.form.get('max_triples'))
    if request.form.get('max_total_resources'):
        app.config['MAX_RESOURCES'] = int(request.form.get('max_total_resources'))
    
    # Initialise a new crawl state
    crawl_id = reset_crawl_state()

    # Make the crawl ID more user-friendly by including the domain of the first seed URL
    if seed_urls:
        try:
            first_url = seed_urls[0]
            parsed_url = urlparse(first_url)
            domain = parsed_url.netloc
            if domain:
                # Update the crawl_id to include the domain name
                new_crawl_id = f"{domain}_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}"
                crawl_state['crawl_id'] = new_crawl_id
                crawl_id = new_crawl_id
        except Exception as e:
            logger.warning(f"Could not create domain-based crawl ID: {str(e)}")

    crawl_state['provenance']['seed_urls'] = seed_urls

    # Check if this is an AJAX request
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        # Start the crawl in a background thread
        thread = threading.Thread(target=perform_crawl, args=(seed_urls,))
        thread.daemon = True  # Thread will terminate when main process exits
        thread.start()
        
        return jsonify({
            'status': 'success', 
            'message': 'Crawl started', 
            'crawl_id': crawl_id
        })
    
    # If not AJAX, start the crawl and render the results page
    try:
        perform_crawl(seed_urls)
    except Exception as e:
        logger.error(f"Error during crawl: {str(e)}")
        logger.error(traceback.format_exc())
    
    return redirect(url_for('results', crawl_id=crawl_id))

def perform_crawl(seed_urls):
    try:
        # Start with the seed URLs as the first level to process
        urls_to_process = seed_urls
        
        # Main crawling loop
        while urls_to_process and should_continue_crawl():
            current_depth = crawl_state['current_depth']
            logger.info(f"Processing {len(urls_to_process)} URLs at depth {current_depth}")
            
            # Track URLs at current level for progress reporting
            crawl_state['current_level_urls'] = urls_to_process.copy()

            # Collect newly discovered URLs at this level
            newly_discovered = []
            
            # Use thread pool for parallel processing if configured
            if app.config.get('USE_PARALLEL', False) and len(urls_to_process) > 1:
                # Use a thread pool to process multiple URLs concurrently
                with ThreadPoolExecutor(max_workers=app.config.get('MAX_WORKERS', 5)) as executor:
                    # Submit each URL to the thread pool
                    future_to_url = {executor.submit(crawl_resource, url, current_depth): url for url in urls_to_process}
                    
                    # Process results as they complete
                    for future in concurrent.futures.as_completed(future_to_url):
                        url = future_to_url[future]
                        try:
                            discovered = future.result()
                            newly_discovered.extend(discovered)
                        except Exception as e:
                            logger.error(f"Error crawling resource {url}: {str(e)}")
                            logger.error(traceback.format_exc())
            else:
                # Sequential processing - one URL at a time
                for url in urls_to_process:
                    try:
                        discovered = crawl_resource(url, current_depth)
                        newly_discovered.extend(discovered)
                    except Exception as e:
                        logger.error(f"Error crawling resource {url}: {str(e)}")
                        logger.error(traceback.format_exc())
            
            # Select resources for next level
            urls_to_process = select_next_resources(newly_discovered)
            logger.info(f"Selected {len(urls_to_process)} URLs for next level (depth {current_depth + 1})")
            
            # Update domain statistics
            for url in urls_to_process:
                domain = urlparse(url).netloc
                crawl_state['provenance']['domains_visited'].add(domain)
            
            # Move to the next depth level
            crawl_state['current_depth'] += 1
            
            # Periodically save provenance information
            if current_depth % 2 == 0:
                try:
                    # Create a temporary "in progress" provenance
                    temp_prov_graph = export_provenance()
                    store_in_fuseki(temp_prov_graph, f"http://example.org/provenance/{crawl_state['crawl_id']}/interim")
                    logger.info(f"Saved interim provenance at depth {current_depth}")
                except Exception as prov_e:
                    logger.error(f"Error saving interim provenance: {str(prov_e)}")
        
        # Finalise crawl
        crawl_state['crawl_active'] = False
        crawl_state['provenance']['finished'] = datetime.datetime.now().isoformat()
        
        # Export and store final provenance
        prov_graph = export_provenance()
        store_in_fuseki(prov_graph, f"http://example.org/provenance/{crawl_state['crawl_id']}")
        
        # Create a standalone provenance file
        try:
            prov_file = os.path.join('static', 'exports', f"provenance-{crawl_state['crawl_id']}.ttl")
            os.makedirs(os.path.dirname(prov_file), exist_ok=True)
            with open(prov_file, 'wb') as f:
                f.write(prov_graph.serialize(format='turtle').encode('utf-8'))
            logger.info(f"Saved provenance to file: {prov_file}")
        except Exception as file_e:
            logger.error(f"Error saving provenance to file: {str(file_e)}")
    
    except Exception as e:
        logger.error(f"Error during crawl: {str(e)}")
        logger.error(traceback.format_exc())
        # Ensure the crawl is marked as inactive and record the error
        crawl_state['crawl_active'] = False
        crawl_state['provenance']['finished'] = datetime.datetime.now().isoformat()
        crawl_state['provenance']['error'] = str(e)
    
    logger.info(f"Crawl {crawl_state['crawl_id']} finished")

@app.route('/results/<crawl_id>')
def results(crawl_id):
    if crawl_state.get('crawl_id') != crawl_id:
        return "Crawl not found", 404
    
    # Query Fuseki for statistics about the crawl results
    try:
        sparql = SPARQLWrapper(f"{app.config['FUSEKI_ENDPOINT']}/{app.config['FUSEKI_DATASET']}/query")
        sparql.setReturnFormat(JSON)
        
        # Query for total triples
        sparql.setQuery("""
        SELECT (COUNT(*) AS ?count) 
        WHERE { 
            ?s ?p ?o 
        }
        """)
        
        results = sparql.query().convert()
        total_triples = results['results']['bindings'][0]['count']['value']
        
        # Query for total graphs
        sparql.setQuery("""
        SELECT (COUNT(DISTINCT ?g) AS ?count) 
        WHERE { 
            GRAPH ?g { ?s ?p ?o } 
        }
        """)
        
        results = sparql.query().convert()
        total_graphs = results['results']['bindings'][0]['count']['value']
        
        # Query for a sample of resources
        sparql.setQuery("""
        SELECT DISTINCT ?resource ?type
        WHERE { 
            ?resource a ?type .
            FILTER(STRSTARTS(STR(?resource), "http://") && !STRSTARTS(STR(?resource), "http://example.org/"))
        }
        LIMIT 20
        """)
        
        results = sparql.query().convert()
        sample_resources = []
        for item in results['results']['bindings']:
            sample_resources.append({
                'uri': item['resource']['value'],
                'type': item['type']['value']
            })
        
        # Query for predicates used
        sparql.setQuery("""
        SELECT ?p (COUNT(*) AS ?count) 
        WHERE { 
            ?s ?p ?o 
        }
        GROUP BY ?p
        ORDER BY DESC(?count)
        LIMIT 20
        """)
        
        results = sparql.query().convert()
        predicate_stats = []
        for item in results['results']['bindings']:
            predicate_stats.append({
                'predicate': item['p']['value'],
                'count': int(item['count']['value'])
            })
        
        # Get domain information from crawl state
        domain_counts = {}
        for domain in crawl_state['provenance'].get('domains_visited', set()):
            domain_counts[domain] = domain_counts.get(domain, 0) + 1
        
        # Calculate the total crawl duration
        if crawl_state['provenance'].get('finished') and crawl_state['provenance'].get('started'):
            try:
                start_time = datetime.datetime.fromisoformat(crawl_state['provenance']['started'])
                end_time = datetime.datetime.fromisoformat(crawl_state['provenance']['finished'])
                duration = end_time - start_time
                duration_str = str(duration)
            except:
                duration_str = "Unknown"
        else:
            duration_str = "In progress"
        
    except Exception as e:
        logger.error(f"Error querying Fuseki: {str(e)}")
        total_triples = "Error"
        total_graphs = "Error"
        sample_resources = []
        predicate_stats = []
        domain_counts = {}
        duration_str = "Error"
    
    return render_template('results.html',
                           crawl_id=crawl_id,
                           stats=crawl_state['signposting_stats'],
                           provenance=crawl_state['provenance'],
                           total_triples=total_triples,
                           total_graphs=total_graphs,
                           sample_resources=sample_resources,
                           predicate_stats=predicate_stats,
                           domain_counts=domain_counts,
                           duration=duration_str)

@app.route('/query', methods=['GET', 'POST'])
def query():
    results = None
    query_text = "SELECT * WHERE { ?s ?p ?o } LIMIT 10"  # Default example query
    
    if request.method == 'POST':
        query_text = request.form.get('query', '')
        # Execute the SPARQL query against Fuseki
        try:
            sparql = SPARQLWrapper(f"{app.config['FUSEKI_ENDPOINT']}/{app.config['FUSEKI_DATASET']}/query")
            sparql.setReturnFormat(JSON)
            sparql.setQuery(query_text)
            
            results = sparql.query().convert()
        except Exception as e:
            logger.error(f"Query error: {str(e)}")
            results = {'error': str(e)}
    
    # Get a list of available named graphs for the dropdown selector
    graphs = []
    try:
        sparql = SPARQLWrapper(f"{app.config['FUSEKI_ENDPOINT']}/{app.config['FUSEKI_DATASET']}/query")
        sparql.setReturnFormat(JSON)
        sparql.setQuery("SELECT DISTINCT ?g WHERE { GRAPH ?g { ?s ?p ?o } }")
        
        graph_results = sparql.query().convert()
        graphs = [item['g']['value'] for item in graph_results['results']['bindings']]
    except:
        pass
    
    return render_template('query.html', 
                           results=results, 
                           query=query_text,
                           graphs=graphs)

@app.route('/explore/<path:resource_uri>')
def explore_resource(resource_uri):
    # Normalise the resource URI to ensure it's a valid URL
    if not resource_uri.startswith('http://') and not resource_uri.startswith('https://'):
        resource_uri = f"http://{resource_uri}"
    
    # Check if resource_uri is formatted as a Python dict and fix it
    if "{'uri':" in resource_uri:
        try:
            # Extract the actual URI using regex
            import re
            uri_match = re.search(r"'uri':\s*'([^']+)'", resource_uri)
            if uri_match:
                actual_uri = uri_match.group(1)
                resource_uri = f"http://{actual_uri}"
        except Exception as e:
            logger.error(f"Error parsing malformed resource URI: {str(e)}")
    
    try:
        sparql = SPARQLWrapper(f"{app.config['FUSEKI_ENDPOINT']}/{app.config['FUSEKI_DATASET']}/query")
        sparql.setReturnFormat(JSON)
        
        # Query for all properties and values of this resource
        # outbound links
        sparql.setQuery(f"""
        SELECT ?p ?o
        WHERE {{ 
            <{resource_uri}> ?p ?o 
        }}
        LIMIT 100
        """)
        
        outbound = sparql.query().convert()
        
        # Query for resources that reference this resource 
        # inbound links
        sparql.setQuery(f"""
        SELECT ?s ?p
        WHERE {{ 
            ?s ?p <{resource_uri}> 
        }}
        LIMIT 100
        """)
        
        inbound = sparql.query().convert()
        
        # Get relevance score if available
        relevance_score = crawl_state['resource_scores'].get(resource_uri, "Unknown")
        
    except Exception as e:
        logger.error(f"Error exploring resource: {str(e)}")
        outbound = {'error': str(e)}
        inbound = {'error': str(e)}
        relevance_score = "Error"
    
    return render_template('explore.html',
                           resource_uri=resource_uri,
                           outbound=outbound,
                           inbound=inbound,
                           relevance_score=relevance_score)

@app.route('/continue-crawl', methods=['POST'])
def continue_crawl():
    # Get the resource URI from the form submission
    resource_uri = request.form.get('resource_uri')
    if not resource_uri:
        return jsonify({'error': 'No resource URI provided'}), 400
    
    # Clean the resource URI in case it's malformed
    if "{'uri':" in resource_uri:
        try: # Extract the actual URI from a Python dict representation
            import re
            uri_match = re.search(r"'uri':\s*'([^']+)'", resource_uri)
            if uri_match:
                actual_uri = uri_match.group(1)
                resource_uri = f"http://{actual_uri}"
        except Exception as e:
            logger.error(f"Error parsing malformed resource URI: {str(e)}")
    
    # Mark the crawl as active so UI will show progress indicator
    crawl_state['crawl_active'] = True

    # Start a new thread to crawl from this resource
    thread = threading.Thread(target=continue_crawl_from_resource, args=(resource_uri,))
    thread.daemon = True
    thread.start()
    
    # Redirect to the homepage with progress indicator
    return redirect(url_for('index'))

def continue_crawl_from_resource(resource_uri):
    """Perform a crawl starting from a specific resource."""
    try:
        # Crawl this resource and follow its links
        newly_discovered = crawl_resource(resource_uri, crawl_state['current_depth'])
        
        # Select the most relevant resources for the next level
        urls_to_process = select_next_resources(newly_discovered)
        
        # Crawl one more level from these selected resources
        next_discovered = []
        for url in urls_to_process:
            discovered = crawl_resource(url, crawl_state['current_depth'] + 1)
            next_discovered.extend(discovered)
        
        # Update crawl state
        crawl_state['crawl_active'] = False
        crawl_state['current_depth'] += 1
        
        # Update provenance record 
        crawl_state['provenance']['finished'] = datetime.datetime.now().isoformat()
        
        # Export updated provenance to Fuseki 
        prov_graph = export_provenance()
        store_in_fuseki(prov_graph, f"http://example.org/provenance/{crawl_state['crawl_id']}")
    except Exception as e:
        logger.error(f"Error continuing crawl: {str(e)}")
        crawl_state['crawl_active'] = False

@app.route('/api/crawl-status')
def api_crawl_status():
    # Check if there's no active crawl
    if not crawl_state.get('crawl_active', False):
        # If crawl is complete, return the ID for redirection
        if 'crawl_id' in crawl_state:
            return jsonify({
                'active': False,
                'crawl_id': crawl_state['crawl_id'],
                'progress': 100,
                'message': 'Crawl completed!',
                'complete': True
            })
        else:
            return jsonify({
                'active': False,
                'message': 'No active crawl'
            })
    
    # Calculate progress for active crawls using multiple factors

    # Progress based on current depth vs maximum depth
    max_depth = app.config['MAX_CRAWL_DEPTH']
    current_depth = crawl_state['current_depth']
    depth_progress = min(80, int((current_depth / max_depth) * 80))
    
    # Progress within the current depth level
    # Calculate based on how many URLs we've processed so far at this level
    urls_at_current_depth = len([u for u in crawl_state.get('visited_urls', []) 
                               if u in crawl_state.get('current_level_urls', [])])
    total_urls_at_level = max(1, len(crawl_state.get('current_level_urls', [])))
    
    # Get the timestamp to create a smooth incrementing effect even without real progress
    current_time = datetime.datetime.now()
    elapsed_seconds = (current_time - crawl_state.get('last_status_check', current_time)).total_seconds()
    # Update the last check time
    crawl_state['last_status_check'] = current_time
    
    # Add a small increment based on time
    time_increment = min(5, max(0.5, elapsed_seconds * 0.5)) if depth_progress < 80 else 0
    
    # If found new URLs since last check, give a small boost to show progress
    url_increase = len(crawl_state.get('visited_urls', set())) - crawl_state.get('last_visited_count', 0)
    crawl_state['last_visited_count'] = len(crawl_state.get('visited_urls', set()))
    
    # Combine all progress factors, but cap at 99% for active crawls
    progress = min(99, depth_progress + time_increment + (url_increase * 0.5))
    
    # Override to 100% if crawl is complete
    if not crawl_state.get('crawl_active', True) and crawl_state.get('provenance', {}).get('finished'):
        progress = 100
    
    # Collect key statistics for the response
    resources_visited = len(crawl_state.get('visited_urls', set()))
    triples_collected = crawl_state.get('provenance', {}).get('triples_collected', 0)
    
    # Get recent log entries for the UI display
    recent_logs = crawl_state.get('recent_logs', [])[-10:]  # Last 10 logs
    
    return jsonify({
        'active': True,
        'progress': round(progress),
        'status': f"Crawling at depth {current_depth} of {max_depth}",
        'resources_visited': resources_visited,
        'triples_collected': triples_collected,
        'logs': recent_logs,
        'crawl_id': crawl_state.get('crawl_id', '')
    })

@app.route('/export-provenance')
def export_provenance_route():
    prov_graph = export_provenance()
    turtle_data = prov_graph.serialize(format='turtle')
    # Create a response with the correct MIME type
    response = app.response_class(
        response=turtle_data,
        status=200,
        mimetype='text/turtle'
    )
    response.headers["Content-Disposition"] = f"attachment; filename=provenance-{crawl_state['crawl_id']}.ttl"
    
    return response

@app.route('/export-graph')
def export_graph():
    graph_uri = request.args.get('graph')
    
    if not graph_uri:
        return "No graph URI specified", 400
    
    try:  # Set up SPARQL endpoint for querying the graph
        sparql = SPARQLWrapper(f"{app.config['FUSEKI_ENDPOINT']}/{app.config['FUSEKI_DATASET']}/query")
        sparql.setReturnFormat('turtle')
        
        # Query to get all triples from the entire named graph
        sparql.setQuery(f"""
        CONSTRUCT {{ ?s ?p ?o }}
        WHERE {{ 
            GRAPH <{graph_uri}> {{ ?s ?p ?o }} 
        }}
        """)
        
        turtle_data = sparql.query().convert()
        
        response = app.response_class(
            response=turtle_data,
            status=200,
            mimetype='text/turtle'
        )
        response.headers["Content-Disposition"] = f"attachment; filename=graph-{graph_uri.split('/')[-1]}.ttl"
        
        return response
        
    except Exception as e:
        logger.error(f"Error exporting graph {graph_uri}: {str(e)}")
        return str(e), 500

@app.route('/visualise')
def visualise():
    graph_uri = request.args.get('graph')
    
    if not graph_uri:
        # Get all graphs to display in dropdown
        try:
            sparql = SPARQLWrapper(f"{app.config['FUSEKI_ENDPOINT']}/{app.config['FUSEKI_DATASET']}/query")
            sparql.setReturnFormat(JSON)
            sparql.setQuery("SELECT DISTINCT ?g WHERE { GRAPH ?g { ?s ?p ?o } }")
            
            graph_results = sparql.query().convert()
            graphs = [item['g']['value'] for item in graph_results['results']['bindings']]
            
            return render_template('visualise.html', graphs=graphs)
        except Exception as e:
            logger.error(f"Error getting graphs for visualisation: {str(e)}")
            return str(e), 500
    
    # If a graph was specified, query data for visualisation
    try:
        sparql = SPARQLWrapper(f"{app.config['FUSEKI_ENDPOINT']}/{app.config['FUSEKI_DATASET']}/query")
        sparql.setReturnFormat(JSON)
        
        # Get a subset of triples from the graph
        sparql.setQuery(f"""
        SELECT ?s ?p ?o 
        WHERE {{ 
            GRAPH <{graph_uri}> {{ ?s ?p ?o }} 
        }}
        LIMIT 100
        """)
        
        results = sparql.query().convert()
        
        # Process the data into a format suitable for visualisation
        nodes = set()
        links = []
        # Extract subject, predicate, object from each triple
        for binding in results['results']['bindings']:
            s = binding['s']['value']
            p = binding['p']['value']
            o = binding['o']['value']
            
            nodes.add(s) # Add the subject to nodes
            links.append({'source': s, 'target': o, 'label': p})
            
            # Add the object to nodes if it's a URI
            if binding['o'].get('type') == 'uri':
                nodes.add(o)

        # Format data for D3.js visualisation        
        graph_data = {
            'nodes': [{'id': node, 'label': node.split('/')[-1]} for node in nodes],
            'links': links
        }
        
        return render_template('visualise.html', 
                               graph_uri=graph_uri, 
                               graph_data=json.dumps(graph_data))
                               
    except Exception as e:
        logger.error(f"Error visualizing graph {graph_uri}: {str(e)}")
        return str(e), 500
    
@app.route('/check-fuseki')
def check_fuseki():
    try:
        # Send a ping request to Fuseki's admin interface
        response = requests.get(f"{app.config['FUSEKI_ENDPOINT']}/$/ping", timeout=5)
        if response.status_code == 200:
            return jsonify({'status': 'success', 'message': 'Fuseki server is running'})
        else: 
            # Connection reached Fuseki but got an error response
            return jsonify({
                'status': 'error', 
                'message': f'Fuseki server returned status code {response.status_code}'
            })
    except Exception as e:
        return jsonify({'status': 'error', 'message': f'Cannot connect to Fuseki: {str(e)}'})
    
@app.route('/config', methods=['GET', 'POST'])
def configure():
    if request.method == 'POST':
        # Update configuration based on form submission
        app.config['FUSEKI_ENDPOINT'] = request.form.get('fuseki_endpoint', app.config['FUSEKI_ENDPOINT'])
        app.config['FUSEKI_DATASET'] = request.form.get('fuseki_dataset', app.config['FUSEKI_DATASET'])
        app.config['MAX_CRAWL_DEPTH'] = int(request.form.get('max_depth', app.config['MAX_CRAWL_DEPTH']))
        app.config['MAX_RESOURCES_PER_LEVEL'] = int(request.form.get('max_resources', app.config['MAX_RESOURCES_PER_LEVEL']))
        app.config['MAX_RESOURCES'] = int(request.form.get('max_total_resources', app.config['MAX_RESOURCES']))
        app.config['MAX_TRIPLES'] = int(request.form.get('max_triples', app.config['MAX_TRIPLES']))
        app.config['RELEVANCE_THRESHOLD'] = float(request.form.get('relevance_threshold', app.config['RELEVANCE_THRESHOLD']))
        app.config['CRAWL_TIMEOUT'] = int(request.form.get('timeout', app.config['CRAWL_TIMEOUT']))
        app.config['USE_PARALLEL'] = request.form.get('use_parallel') == 'on'
        if request.form.get('max_workers'):
            app.config['MAX_WORKERS'] = int(request.form.get('max_workers'))
        
        return redirect(url_for('index')) # Redirect to homepage after saving configuration
        
    return render_template('config.html', config=app.config)  # For GET requests, display the configuration form

@app.route('/api/graph-data')
def api_graph_data():
    graph_uri = request.args.get('graph')
    
    if not graph_uri:
        return jsonify({'error': 'No graph URI provided'}), 400
    
    # Query data for the specific graph
    try:
        sparql = SPARQLWrapper(f"{app.config['FUSEKI_ENDPOINT']}/{app.config['FUSEKI_DATASET']}/query")
        sparql.setReturnFormat(JSON)
        
        # Get a subset of triples from the graph
        sparql.setQuery(f"""
        SELECT ?s ?p ?o 
        WHERE {{ 
            GRAPH <{graph_uri}> {{ ?s ?p ?o }} 
        }}
        LIMIT 200
        """)
        
        results = sparql.query().convert()
        
        # Format data as a list of triples 
        triples = []
        
        for binding in results['results']['bindings']:
            triples.append({
                'subject': binding['s']['value'],
                'predicate': binding['p']['value'],
                'object': binding['o']['value']
            })
            
        return jsonify(triples)
                               
    except Exception as e:
        logger.error(f"Error getting graph data for {graph_uri}: {str(e)}")
        return jsonify({'error': str(e)}), 500
    
@app.route('/statistics/<crawl_id>')
def statistics(crawl_id):
    if crawl_state.get('crawl_id') != crawl_id:
        return "Crawl not found", 404
    
    # Generate statistics from the crawl data
    try:
        format_stats = {}
        mime_stats = {}
        rel_stats = {}
        domain_stats = {}
        
        # Analyse each resource in the crawl results
        if 'resources' in crawl_state['provenance']:
            for resource in crawl_state['provenance']['resources']:
                # Format stats
                if 'format' in resource:
                    fmt = resource['format']
                    format_stats[fmt] = format_stats.get(fmt, 0) + 1
                
                # MIME type stats
                if 'content_type' in resource:
                    ct = resource['content_type']
                    mime_stats[ct] = mime_stats.get(ct, 0) + 1
                
                # Relation type stats from source_type
                source_type = resource['source_type']
                if source_type.startswith('signposting:'):
                    rel = source_type.split(':')[1]
                    rel_stats[rel] = rel_stats.get(rel, 0) + 1
                else:
                    rel_stats[source_type] = rel_stats.get(source_type, 0) + 1
                
                # Domain stats
                try:
                    domain = urlparse(resource['url']).netloc
                    domain_stats[domain] = domain_stats.get(domain, 0) + 1
                except:
                    pass
        
        # Get direct additional stats from signposting_stats
        mime_type_counts = crawl_state['signposting_stats'].get('mime_types', {})
        rel_type_counts = crawl_state['signposting_stats'].get('rel_types', {})
        
        # Merge with statistics collected above
        for mime, count in mime_type_counts.items():
            mime_stats[mime] = mime_stats.get(mime, 0) + count
        
        for rel, count in rel_type_counts.items():
            rel_stats[rel] = rel_stats.get(rel, 0) + count
        
        # Generate domain distribution chart data
        domain_chart_data = []
        for domain, count in sorted(domain_stats.items(), key=lambda x: x[1], reverse=True)[:10]:
            domain_chart_data.append({
                'domain': domain,
                'count': count
            })
        
        # Format distribution chart data
        format_chart_data = []
        for fmt, count in sorted(format_stats.items(), key=lambda x: x[1], reverse=True):
            format_chart_data.append({
                'format': fmt,
                'count': count
            })
        
        # MIME type distribution chart data
        mime_chart_data = []
        for mime, count in sorted(mime_stats.items(), key=lambda x: x[1], reverse=True):
            mime_chart_data.append({
                'mime': mime,
                'count': count
            })
        
        # Generate relation type distribution chart data
        rel_chart_data = []
        for rel, count in sorted(rel_stats.items(), key=lambda x: x[1], reverse=True):
            rel_chart_data.append({
                'relation': rel,
                'count': count
            })
        
        return render_template('statistics.html',
                               crawl_id=crawl_id,
                               stats=crawl_state['signposting_stats'],
                               provenance=crawl_state['provenance'],
                               format_stats=format_stats,
                               mime_stats=mime_stats,
                               rel_stats=rel_stats,
                               domain_stats=domain_stats,
                               domain_chart_data=json.dumps(domain_chart_data),
                               format_chart_data=json.dumps(format_chart_data),
                               mime_chart_data=json.dumps(mime_chart_data),
                               rel_chart_data=json.dumps(rel_chart_data))
                               
    except Exception as e:
        logger.error(f"Error generating statistics: {str(e)}")
        logger.error(traceback.format_exc())
        return f"Error generating statistics: {str(e)}", 500
    

@app.route('/api/seed-check', methods=['POST'])
def api_seed_check():
    # Get the URL to check from the JSON request
    seed_url = request.json.get('url')
    if not seed_url:
        return jsonify({'error': 'No URL provided'}), 400
    # Initialise result structure
    results = {
        'url': seed_url,
        'score': 0,
        'details': {
            'rdf_found': False,
            'signposting_found': False,
            'known_repository': False,
            'mime_types': [],
            'rel_types': [],
            'formats': []
        }
    }
    
    try:
        # Check for RDF directly at the URL
        try:
            g, error, format_used, content_type = fetch_and_parse_rdf(seed_url)
            if len(g) > 0:
                results['details']['rdf_found'] = True
                results['score'] += 0.5
                
                if format_used:
                    results['details']['formats'].append(format_used)
                
                if content_type:
                    results['details']['mime_types'].append(content_type)
        except Exception as e:
            logger.warning(f"Error checking seed URL for RDF: {str(e)}")
        
        # Check for signposting links
        try:
            links = get_signposting_links(seed_url)
            if links:
                results['details']['signposting_found'] = True
                results['score'] += 0.3
                
                # Track relation types
                for rel in links.keys():
                    if rel not in results['details']['rel_types']:
                        results['details']['rel_types'].append(rel)
        except Exception as e:
            logger.warning(f"Error checking seed URL for signposting: {str(e)}")
        
        # Check if it's a known data repository
        known_repositories = [
            'zenodo.org', 'figshare.com', 'datadryad.org', 'dataverse', 'ands.org.au',
            'doi.org', 'datacite.org', 'pangaea.de', 'ncbi.nlm.nih.gov', 'orcid.org',
            'github.com', 'gitlab.com', 'bitbucket.org', 'sourceforge.net'
        ]
        
        if any(repo in seed_url.lower() for repo in known_repositories):
            results['details']['known_repository'] = True
            results['score'] += 0.2
        
        # Apply a scoring threshold
        if results['score'] >= 0.3:
            results['recommendation'] = 'promising'
        elif results['score'] > 0:
            results['recommendation'] = 'might_work'
        else:
            results['recommendation'] = 'unlikely'
        
        return jsonify(results)
        
    except Exception as e:
        logger.error(f"Error in seed check: {str(e)}")
        return jsonify({
            'error': str(e),
            'url': seed_url,
            'score': 0,
            'recommendation': 'error'
        }), 500

@app.route('/fair-assessment/<path:resource_uri>')
def fair_assessment(resource_uri):
    resource_uri = f"http://{resource_uri}"  # Add back the protocol if missing
    # Initialise assessment structure with categories and scores
    assessment = {
        'url': resource_uri,
        'timestamp': datetime.datetime.now().isoformat(),
        'findable': {
            'score': 0,  # Current score for Findable criteria
            'max': 5,   # Maximum possible score
            'details': []  # List of criteria that were met
        },
        'accessible': {
            'score': 0,
            'max': 5,
            'details': []
        },
        'interoperable': {
            'score': 0,
            'max': 5,
            'details': []
        },
        'reusable': {
            'score': 0,
            'max': 5,
            'details': []
        }
    }
    
    try:
        graph = Graph()
        # Try loading from Fuseki first if available
        try:
            sparql = SPARQLWrapper(f"{app.config['FUSEKI_ENDPOINT']}/{app.config['FUSEKI_DATASET']}/query")
            sparql.setReturnFormat('turtle')
            
            # Query for triples about this resource (either as subject or object)
            sparql.setQuery(f"""
            CONSTRUCT {{ ?s ?p ?o }}
            WHERE {{ 
                {{ <{resource_uri}> ?p ?o }} UNION {{ ?s ?p <{resource_uri}> }}
            }}
            """)
            
            try:
                turtle_data = sparql.query().convert() # Try to parse the result as Turtle
                graph.parse(data=turtle_data, format='turtle')
                
                # If we got data, improve the findable score
                if len(graph) > 0:
                    assessment['findable']['score'] += 1
                    assessment['findable']['details'].append("Resource is indexed in RDF store")
            except:
                # If Fuseki returned data but parsing failed, try direct HTTP
                g, error, format_used, content_type = fetch_and_parse_rdf(resource_uri)
                if len(g) > 0:
                    graph = g
        except:
            # If Fuseki query fails, try direct HTTP request
            g, error, format_used, content_type = fetch_and_parse_rdf(resource_uri)
            if len(g) > 0:
                graph = g
        
        # Check HTTP headers for Findability
        try:
            headers = {
                'Accept': 'application/rdf+xml, text/turtle, application/ld+json, text/n3, application/n-triples'
            }
            response = requests.head(resource_uri, allow_redirects=True, timeout=10, headers=headers)
            
            # Check for signposting links
            if 'Link' in response.headers:
                assessment['findable']['score'] += 1
                assessment['findable']['details'].append("Resource uses HTTP Link headers for Signposting")
                
                # Parse links
                link_header = response.headers['Link']
                link_matches = re.findall(r'<([^>]*)>\s*;\s*rel=(?:"([^"]*)"|([^,\s]*))', link_header)
                
                # Check for specific relation types
                for target, rel1, rel2 in link_matches:
                    rel = rel1 if rel1 else rel2
                    
                    # Check for specific links that improve FAIR
                    if rel == 'describedby':
                        assessment['findable']['score'] += 1
                        assessment['findable']['details'].append("Resource links to its metadata (describedby)")
                    
                    if rel == 'license':
                        assessment['reusable']['score'] += 1
                        assessment['reusable']['details'].append("Resource links to license information")
                    
                    if rel == 'type':
                        assessment['interoperable']['score'] += 1
                        assessment['interoperable']['details'].append("Resource specifies its type")
                    
                    if rel == 'cite-as':
                        assessment['reusable']['score'] += 1
                        assessment['reusable']['details'].append("Resource provides citation information")
            
            # Check for content negotiation support (Accessibility)
            accept_types = [
                'application/rdf+xml', 
                'text/turtle', 
                'application/ld+json', 
                'application/n-triples'
            ]
            
            content_neg_support = False
            for accept_type in accept_types:
                try:
                    h = {'Accept': accept_type}
                    r = requests.head(resource_uri, headers=h, timeout=5)
                    if r.status_code == 200 and accept_type in r.headers.get('Content-Type', ''):
                        content_neg_support = True
                        assessment['accessible']['score'] += 1
                        assessment['accessible']['details'].append(f"Supports content negotiation for {accept_type}")
                        break
                except:
                    pass
            
            if not content_neg_support:
                # At least check if direct access works
                if response.status_code == 200:
                    assessment['accessible']['score'] += 1
                    assessment['accessible']['details'].append("Resource is accessible via HTTP")
        
        except Exception as e:
            logger.error(f"Error checking HTTP headers: {str(e)}")
        
        # Check RDF content for FAIR criteria
        if len(graph) > 0:
            # Check for persistent identifiers
            persistent_id_patterns = [
                'doi\.org', 'handle\.net', 'purl\.org', 'w3id\.org', 
                'identifiers\.org', 'orcid\.org'
            ]
            
            found_persistent_id = False
            for s, p, o in graph:
                for pattern in persistent_id_patterns:
                    for uri in [s, p, o]:
                        if isinstance(uri, URIRef) and re.search(pattern, str(uri)):
                            found_persistent_id = True
                            assessment['findable']['score'] += 1
                            assessment['findable']['details'].append(f"Uses persistent identifier: {pattern}")
                            break
                    if found_persistent_id:
                        break
                if found_persistent_id:
                    break
            
            # Check for common metadata vocabularies (Interoperability)
            metadata_vocabs = {
                'http://schema.org/': 'Schema.org',
                'http://purl.org/dc/': 'Dublin Core',
                'http://www.w3.org/ns/dcat': 'DCAT',
                'http://xmlns.com/foaf/': 'FOAF',
                'http://rdfs.org/ns/void': 'VoID',
                'http://www.w3.org/2004/02/skos/': 'SKOS'
            }
            
            found_vocabs = set()
            for s, p, o in graph:
                for vocab_url, vocab_name in metadata_vocabs.items():
                    if vocab_url in str(p):
                        found_vocabs.add(vocab_name)
            
            if found_vocabs:
                assessment['interoperable']['score'] = min(3, len(found_vocabs))
                for vocab in found_vocabs:
                    assessment['interoperable']['details'].append(f"Uses standardized vocabulary: {vocab}")
            
            # Check for license information (Reusability)
            license_predicates = [
                URIRef('http://purl.org/dc/terms/license'),
                URIRef('http://schema.org/license'),
                URIRef('http://www.w3.org/1999/xhtml/vocab#license'),
                URIRef('http://creativecommons.org/ns#license')
            ]
            
            for p in license_predicates:
                if (URIRef(resource_uri), p, None) in graph:
                    assessment['reusable']['score'] += 1
                    assessment['reusable']['details'].append("Contains license information in RDF")
                    break
            
            # Check for provenance information (Reusability)
            provenance_predicates = [
                URIRef('http://purl.org/dc/terms/provenance'),
                URIRef('http://purl.org/dc/terms/source'),
                URIRef('http://www.w3.org/ns/prov#wasGeneratedBy'),
                URIRef('http://www.w3.org/ns/prov#wasDerivedFrom')
            ]
            
            for p in provenance_predicates:
                if (URIRef(resource_uri), p, None) in graph:
                    assessment['reusable']['score'] += 1
                    assessment['reusable']['details'].append("Contains provenance information")
                    break
            
            # Check for machine-readable data (Interoperability)
            if format_used in ['turtle', 'xml', 'json-ld', 'n3', 'nt']:
                assessment['interoperable']['score'] += 1
                assessment['interoperable']['details'].append(f"Resource available in machine-readable format: {format_used}")
        
        # Calculate overall FAIR score
        max_score = assessment['findable']['max'] + assessment['accessible']['max'] + \
                    assessment['interoperable']['max'] + assessment['reusable']['max']
        
        actual_score = assessment['findable']['score'] + assessment['accessible']['score'] + \
                       assessment['interoperable']['score'] + assessment['reusable']['score']
        
        # Cap individual scores to their maximums
        for category in ['findable', 'accessible', 'interoperable', 'reusable']:
            assessment[category]['score'] = min(assessment[category]['score'], assessment[category]['max'])
            
        assessment['overall'] = {
            'score': actual_score,
            'max': max_score,
            'percentage': round((actual_score / max_score) * 100)
        }
        
        return render_template('fair_assessment.html', 
                               resource_uri=resource_uri, 
                               assessment=assessment)
    
    except Exception as e:
        logger.error(f"Error during FAIR assessment for {resource_uri}: {str(e)}")
        logger.error(traceback.format_exc())
        return f"Error during FAIR assessment: {str(e)}", 500

@app.route('/check-environment')
def check_environment():
    # Initialise check results structure with default "unknown" status
    checks = {
        'fuseki': {
            'status': 'unknown',  # Will be set to 'ok', 'warning', or 'error'
            'message': '', # Descriptive message about the check
            'details': {}  # Additional details about the check
        },
        'modules': {
            'status': 'unknown',
            'message': '',
            'details': {}
        },
        'rdf_formats': {
            'status': 'unknown',
            'message': '',
            'details': {}
        },
        'network': {
            'status': 'unknown',
            'message': '',
            'details': {}
        },
        'storage': {
            'status': 'unknown',
            'message': '',
            'details': {}
        }
    }
    
    # Check Fuseki connection
    try:
        response = requests.get(f"{app.config['FUSEKI_ENDPOINT']}/$/ping", timeout=5)
        if response.status_code == 200:
            checks['fuseki']['status'] = 'ok'
            checks['fuseki']['message'] = 'Connected to Fuseki'
            
            # Check for dataset
            try:
                # Query the list of datasets from Fuseki
                dataset_response = requests.get(f"{app.config['FUSEKI_ENDPOINT']}/$/datasets", timeout=5)
                if dataset_response.status_code == 200:
                    datasets = dataset_response.json().get('datasets', [])
                    dataset_found = False
                    for ds in datasets:
                        if ds.get('ds.name') == f"/{app.config['FUSEKI_DATASET']}":
                            dataset_found = True
                            checks['fuseki']['details']['dataset'] = 'found'
                            break
                    
                    if not dataset_found: # If dataset not found, change status to warning
                        checks['fuseki']['status'] = 'warning'
                        checks['fuseki']['message'] = f"Dataset '{app.config['FUSEKI_DATASET']}' not found in Fuseki"
                        checks['fuseki']['details']['dataset'] = 'not found'
                        # List available datasets for diagnostic purposes
                        checks['fuseki']['details']['available_datasets'] = [ds.get('ds.name', '').strip('/') for ds in datasets]
            except Exception as ds_e:
                checks['fuseki']['status'] = 'warning'
                checks['fuseki']['message'] = f"Connected to Fuseki but couldn't check datasets: {str(ds_e)}"
        else:
            checks['fuseki']['status'] = 'error'
            checks['fuseki']['message'] = f"Fuseki server returned status code {response.status_code}"
    except Exception as e:
        checks['fuseki']['status'] = 'error'
        checks['fuseki']['message'] = f"Cannot connect to Fuseki: {str(e)}"
    
    # Check required and optional Python modules
    required_modules = {
        'rdflib': False,
        'requests': False,
        'bs4': False,
        'SPARQLWrapper': False,
        'flask': False
    }
    
    optional_modules = {
        'rdflib_microdata': False,
        'concurrent.futures': False
    }
    
    try:
        # Check each required module
        import rdflib
        required_modules['rdflib'] = True
        
        import requests
        required_modules['requests'] = True
        
        import bs4
        required_modules['bs4'] = True
        
        import SPARQLWrapper
        required_modules['SPARQLWrapper'] = True
        
        import flask
        required_modules['flask'] = True

        # Check optional modules
        try:
            import rdflib_microdata
            optional_modules['rdflib_microdata'] = True
        except ImportError:
            pass # Optional, so continue if not available
            
        try:
            import concurrent.futures
            optional_modules['concurrent.futures'] = True
        except ImportError:
            pass # Optional, so continue if not available
        
        # Check module versions
        checks['modules']['details']['rdflib_version'] = rdflib.__version__
        checks['modules']['details']['requests_version'] = requests.__version__
        checks['modules']['details']['bs4_version'] = bs4.__version__
        checks['modules']['details']['SPARQLWrapper_version'] = SPARQLWrapper.__version__
        checks['modules']['details']['flask_version'] = flask.__version__
        
        # Determine overall module check status
        if all(required_modules.values()):
            checks['modules']['status'] = 'ok'
            checks['modules']['message'] = 'All required modules are present'
        else:
            missing = [m for m, present in required_modules.items() if not present]
            checks['modules']['status'] = 'error'
            checks['modules']['message'] = f"Missing required modules: {', '.join(missing)}"
        # Store module check details
        checks['modules']['details']['required'] = required_modules
        checks['modules']['details']['optional'] = optional_modules
        
    except Exception as mod_e:
        checks['modules']['status'] = 'error'
        checks['modules']['message'] = f"Error checking modules: {str(mod_e)}"
    
    # Check supported RDF formats
    try:
        # Get list of parsers and serialisers from RDFLib's plugin system
        parsers = list(plugin.name for plugin in rdflib.plugin.plugins(kind=rdflib.plugin.Parser))
        serializers = list(plugin.name for plugin in rdflib.plugin.plugins(kind=rdflib.plugin.Serializer))
        # Store format lists for display
        checks['rdf_formats']['details']['parsers'] = parsers
        checks['rdf_formats']['details']['serializers'] = serializers
        
        # Check if essential formats are available
        essential_formats = ['xml', 'turtle', 'json-ld', 'n3', 'nt']
        missing_formats = [fmt for fmt in essential_formats if fmt not in parsers]
        # Determine format check status
        if not missing_formats:
            checks['rdf_formats']['status'] = 'ok'
            checks['rdf_formats']['message'] = 'All essential RDF formats are supported'
        else:
            checks['rdf_formats']['status'] = 'warning'
            checks['rdf_formats']['message'] = f"Missing support for some RDF formats: {', '.join(missing_formats)}"
    except Exception as fmt_e:
        checks['rdf_formats']['status'] = 'error'
        checks['rdf_formats']['message'] = f"Error checking RDF formats: {str(fmt_e)}"
    
    # Check network connectivity
    test_urls = [
        'https://www.google.com', # General internet access
        'https://schema.org',    # Schema.org vocabulary
        'https://www.w3.org',  # W3C home (for RDF standards)
        'https://doi.org'      # DOI resolution
    ]
    
    network_results = {}
    network_ok = True

    # Test each URL
    for url in test_urls:
        try:
            response = requests.head(url, timeout=5)
            network_results[url] = {
                'status': response.status_code,
                'ok': response.status_code == 200
            }
            if response.status_code != 200:
                network_ok = False
        except Exception as net_e:
            network_results[url] = {
                'status': 'error',
                'message': str(net_e),
                'ok': False
            }
            network_ok = False
    
    checks['network']['details'] = network_results
    # Determine network check status
    if network_ok:
        checks['network']['status'] = 'ok'
        checks['network']['message'] = 'Network connectivity looks good'
    else:
        failed = [url for url, result in network_results.items() if not result.get('ok')]
        if len(failed) == len(test_urls):
            checks['network']['status'] = 'error'
            checks['network']['message'] = 'Cannot connect to any test sites - check network connection'
        else:
            checks['network']['status'] = 'warning'
            checks['network']['message'] = f"Some network tests failed: {', '.join(failed)}"
    
    # Check storage access and space
    try:
        exports_dir = os.path.join('static', 'exports')
        os.makedirs(exports_dir, exist_ok=True)
        
        # Try to write a test file to verify permissions
        test_file = os.path.join(exports_dir, 'test_write.tmp')
        with open(test_file, 'w') as f:
            f.write('Test file for write permissions')
        
        # Check if we can read it back
        with open(test_file, 'r') as f:
            content = f.read()
        os.remove(test_file) #Clean up the test file
        
        # Check available disk space (Unix systems only)
        if hasattr(os, 'statvfs'):
            stats = os.statvfs(exports_dir)
            free_space = stats.f_frsize * stats.f_bavail
            total_space = stats.f_frsize * stats.f_blocks
            
            checks['storage']['details']['free_space'] = f"{free_space / (1024*1024*1024):.2f} GB"
            checks['storage']['details']['total_space'] = f"{total_space / (1024*1024*1024):.2f} GB"
            
            if free_space < 100 * 1024 * 1024:  # Less than 100MB
                checks['storage']['status'] = 'error'
                checks['storage']['message'] = 'Very low disk space available'
            elif free_space < 1024 * 1024 * 1024:  # Less than 1GB
                checks['storage']['status'] = 'warning'
                checks['storage']['message'] = 'Low disk space available'
            else:
                checks['storage']['status'] = 'ok'
                checks['storage']['message'] = 'Storage looks good'
        else:
            # For systems that don't support statvfs (e.g., Windows)
            checks['storage']['status'] = 'ok'
            checks['storage']['message'] = 'Storage is writable (disk space unknown)'
    except Exception as storage_e:
        checks['storage']['status'] = 'error'
        checks['storage']['message'] = f"Storage error: {str(storage_e)}"
    
    # Return the status page
    return render_template('environment_check.html', checks=checks)

@app.route('/export-knowledge-graph')
def export_knowledge_graph():
    format_param = request.args.get('format', 'turtle')
    
    # Map format parameter to actual RDFLib format name
    format_map = {
        'turtle': 'turtle',
        'ttl': 'turtle',
        'rdf': 'xml',
        'xml': 'xml',
        'json-ld': 'json-ld',
        'jsonld': 'json-ld',
        'n3': 'n3',
        'nt': 'nt',
        'ntriples': 'nt',
        'nquads': 'nquads'
    }
    
    export_format = format_map.get(format_param, 'turtle') # Resolve the requested format to RDFLib format name
    
    # Map formats to appropriate MIME types for HTTP response
    mime_types = {
        'turtle': 'text/turtle',
        'xml': 'application/rdf+xml',
        'json-ld': 'application/ld+json',
        'n3': 'text/n3',
        'nt': 'application/n-triples',
        'nquads': 'application/n-quads'
    }
    
    mime_type = mime_types.get(export_format, 'text/turtle')
    
    # Get the named graph parameter if provided
    graph_uri = request.args.get('graph', None)
    
    try:
        sparql = SPARQLWrapper(f"{app.config['FUSEKI_ENDPOINT']}/{app.config['FUSEKI_DATASET']}/query")
        
        # Special handling for N-Quads format which preserves graph context
        if export_format == 'nquads':
            sparql.setReturnFormat('json')
            
            # Query triples from specific graph
            if graph_uri:
                sparql.setQuery(f"""
                SELECT ?s ?p ?o
                WHERE {{ 
                    GRAPH <{graph_uri}> {{ ?s ?p ?o }} 
                }}
                """)
            else:
                # Query all triples with their graph context
                sparql.setQuery("""
                SELECT ?g ?s ?p ?o
                WHERE { 
                    GRAPH ?g { ?s ?p ?o } 
                }
                """)
            
            results = sparql.query().convert()
            
            # Manually construct N-Quads format from results 
            quads = []
            for binding in results['results']['bindings']:
                s = binding['s']['value']
                p = binding['p']['value']
                o = binding['o']['value']
                
                # Format subject based on type (URI or blank node)
                if binding['s']['type'] == 'uri':
                    s_formatted = f"<{s}>"
                else:
                    # Blank nodes have _: prefix
                    s_formatted = f"_:{s}"
                
                # Format predicate (always a URI)
                p_formatted = f"<{p}>"
                
                # Format object based on type (URI, blank node, or literal)
                if binding['o']['type'] == 'uri':
                    o_formatted = f"<{o}>"
                elif binding['o']['type'] == 'bnode':
                    o_formatted = f"_:{o}"
                else:
                    # literal - handle datatype and language tags
                    if 'datatype' in binding['o']:
                        datatype = binding['o']['datatype']
                        o_formatted = f'"{o}"^^<{datatype}>' # Typed literal
                    elif 'xml:lang' in binding['o']:
                        lang = binding['o']['xml:lang']
                        o_formatted = f'"{o}"@{lang}'  # Language-tagged literal
                    else:
                        o_formatted = f'"{o}"'  # Plain literal
                
                # Format graph name
                if graph_uri:
                    g_formatted = f"<{graph_uri}>"
                else:
                    g = binding['g']['value']
                    g_formatted = f"<{g}>"
                
                # Construct the complete quad in N-Quads format
                quad = f"{s_formatted} {p_formatted} {o_formatted} {g_formatted} ."
                quads.append(quad)
            
            # Join all quads with newlines
            data = '\n'.join(quads)
            
        else:
            # For other formats, we can use CONSTRUCT query to get the triples
            sparql.setReturnFormat(export_format)
            
            if graph_uri:
                sparql.setQuery(f"""
                CONSTRUCT {{ ?s ?p ?o }}
                WHERE {{ 
                    GRAPH <{graph_uri}> {{ ?s ?p ?o }} 
                }}
                """)
            else:
                sparql.setQuery("""
                CONSTRUCT { ?s ?p ?o }
                WHERE { 
                    GRAPH ?g { ?s ?p ?o } 
                }
                """)
            
            data = sparql.query().convert()
        
        # Create the HTTP response with appropriate filename
        filename = f"knowledge_graph_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.{format_param}"
        response = app.response_class(
            response=data,
            status=200,
            mimetype=mime_type
        )
        response.headers["Content-Disposition"] = f"attachment; filename={filename}"
        
        return response
        
    except Exception as e:
        logger.error(f"Error exporting knowledge graph: {str(e)}")
        logger.error(traceback.format_exc())
        return f"Error exporting knowledge graph: {str(e)}", 500
    
if __name__ == '__main__':
    # Print startup information
    print(f"FAIR Signposting Crawler starting up...")
    print(f"Fuseki endpoint: {app.config['FUSEKI_ENDPOINT']}")
    print(f"Fuseki dataset: {app.config['FUSEKI_DATASET']}")
    
    # Check if Fuseki is running and accessible
    try:
        response = requests.get(f"{app.config['FUSEKI_ENDPOINT']}/$/ping", timeout=5)
        if response.status_code == 200:
            print("✓ Connected to Fuseki")
        else:
            print(f"⚠ Fuseki returned status code {response.status_code}")
    except Exception as e:
        print(f"⚠ Cannot connect to Fuseki: {str(e)}")
        print(f"  Make sure Fuseki is running at {app.config['FUSEKI_ENDPOINT']}")
    
    # Check for supported RDF formats from RDFLib
    try:
        supported_formats = [format for format in (plugin.name for plugin in rdflib.plugin.plugins(kind=rdflib.plugin.Parser))]
        print(f"✓ RDFlib loaded with {len(supported_formats)} parser formats")
    except Exception as e:
        print(f"⚠ Error checking RDF formats: {str(e)}")
    
    # Check for optional modules that enhance functionality
    if HAS_MICRODATA:
        print("✓ rdflib_microdata extension loaded (Microdata/Schema.org support enabled)")
    else:
        print("ℹ rdflib_microdata not found (Microdata/Schema.org support limited)")
    
    # Start the web application
    print(f"Starting web server on http://0.0.0.0:5000/")
    app.run(debug=True, host='0.0.0.0', port=5000)
