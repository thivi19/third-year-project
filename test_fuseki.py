import requests
import rdflib
from rdflib import Graph, URIRef, Literal
from rdflib.namespace import RDF, RDFS
import logging
import json

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Configuration
FUSEKI_ENDPOINT = 'http://localhost:3030'
FUSEKI_DATASET = 'knowledge_graph'

def test_fuseki_connection():
    """Test basic connection to Fuseki server"""
    try:
        response = requests.get(f"{FUSEKI_ENDPOINT}/$/ping", timeout=5)
        if response.status_code == 200:
            logger.info("✓ Successfully connected to Fuseki server")
            return True
        else:
            logger.error(f"✗ Fuseki server returned status code {response.status_code}")
            return False
    except Exception as e:
        logger.error(f"✗ Cannot connect to Fuseki: {str(e)}")
        return False

def test_dataset_exists():
    """Test if the dataset exists in Fuseki"""
    try:
        response = requests.get(f"{FUSEKI_ENDPOINT}/$/datasets", timeout=5)
        if response.status_code == 200:
            datasets = response.json().get('datasets', [])
            dataset_found = False
            for ds in datasets:
                if ds.get('ds.name') == f"/{FUSEKI_DATASET}":
                    dataset_found = True
                    logger.info(f"✓ Dataset '{FUSEKI_DATASET}' exists in Fuseki")
                    break
            
            if not dataset_found:
                logger.error(f"✗ Dataset '{FUSEKI_DATASET}' not found in Fuseki")
                logger.info(f"Available datasets: {[ds.get('ds.name', '').strip('/') for ds in datasets]}")
                return False
            return True
        else:
            logger.error(f"✗ Failed to get datasets list: {response.status_code}")
            return False
    except Exception as e:
        logger.error(f"✗ Error checking datasets: {str(e)}")
        return False

def test_direct_query():
    """Test a simple SPARQL query directly using HTTP"""
    try:
        query = "SELECT * WHERE { ?s ?p ?o } LIMIT 10"
        headers = {
            'Content-Type': 'application/sparql-query',
            'Accept': 'application/sparql-results+json'
        }
        response = requests.post(
            f"{FUSEKI_ENDPOINT}/{FUSEKI_DATASET}/query",
            data=query,
            headers=headers,
            timeout=10
        )
        
        if response.status_code == 200:
            logger.info("✓ Successfully executed SPARQL query")
            result = response.json()
            binding_count = len(result.get('results', {}).get('bindings', []))
            logger.info(f"  Got {binding_count} results")
            return True
        else:
            logger.error(f"✗ Query failed: {response.status_code} - {response.text}")
            return False
    except Exception as e:
        logger.error(f"✗ Error executing query: {str(e)}")
        return False

def test_direct_update():
    """Test a simple SPARQL update directly using HTTP"""
    try:
        # Create a test triple
        update_query = """
        PREFIX dc: <http://purl.org/dc/elements/1.1/>
        INSERT DATA { 
          <http://example.org/test> dc:title "Test Resource" .
        }
        """
        headers = {
            'Content-Type': 'application/sparql-update',
        }
        response = requests.post(
            f"{FUSEKI_ENDPOINT}/{FUSEKI_DATASET}/update",
            data=update_query,
            headers=headers,
            timeout=10
        )
        
        if response.status_code == 200 or response.status_code == 204:
            logger.info("✓ Successfully executed SPARQL update")
            return True
        else:
            logger.error(f"✗ Update failed: {response.status_code} - {response.text}")
            return False
    except Exception as e:
        logger.error(f"✗ Error executing update: {str(e)}")
        return False

def test_rdf_storage():
    """Test storing a simple RDF graph in Fuseki"""
    try:
        # Create a simple RDF graph
        g = Graph()
        test_subject = URIRef("http://example.org/test-subject")
        test_predicate = URIRef("http://example.org/test-predicate")
        test_object = Literal("Test Object")
        
        g.add((test_subject, test_predicate, test_object))
        g.add((test_subject, RDF.type, RDFS.Resource))
        
        # Serialise to N-Triples
        ntriples_data = g.serialize(format='nt')
        if isinstance(ntriples_data, bytes):
            ntriples_data = ntriples_data.decode('utf-8')
        
        # Construct update query
        update_query = f"""
        INSERT DATA {{ 
            GRAPH <http://example.org/test-graph> {{ 
                {ntriples_data}
            }}
        }}
        """
        
        headers = {
            'Content-Type': 'application/sparql-update',
        }
        response = requests.post(
            f"{FUSEKI_ENDPOINT}/{FUSEKI_DATASET}/update",
            data=update_query,
            headers=headers,
            timeout=10
        )
        
        if response.status_code == 200 or response.status_code == 204:
            logger.info("✓ Successfully stored RDF graph in Fuseki")
            return True
        else:
            logger.error(f"✗ RDF storage failed: {response.status_code} - {response.text}")
            return False
    except Exception as e:
        logger.error(f"✗ Error storing RDF: {str(e)}")
        return False

if __name__ == "__main__":
    print("=== Fuseki Connection Test ===")
    
    # Run tests
    connection_ok = test_fuseki_connection()
    if connection_ok:
        dataset_ok = test_dataset_exists()
        if dataset_ok:
            query_ok = test_direct_query()
            update_ok = test_direct_update()
            rdf_ok = test_rdf_storage()
            
            if query_ok and update_ok and rdf_ok:
                print("\n✓ All tests PASSED! Fuseki is working correctly.")
            else:
                print("\n⚠ Some tests FAILED. See log for details.")
        else:
            print(f"\n⚠ Dataset '{FUSEKI_DATASET}' not found. Create it in Fuseki first.")
    else:
        print("\n⚠ Could not connect to Fuseki server. Check if it's running.")