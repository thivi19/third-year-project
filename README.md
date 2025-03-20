# third-year-project
# FAIR Signposting Crawler
A web-based tool for crawling and analysing linked data resources that follow FAIR (Findable, Accessible, Interoperable, Reusable) principles through Signposting links and other RDF discovery mechanisms.

## Overview
The FAIR Signposting Crawler is a Python-based application that explores the web following FAIR Signposting links, builds a knowledge graph from discovered RDF data, and provides tools for querying, visualisation, and FAIR assessment of linked data resources.
Signposting is a simple approach to make navigational links explicit for machine consumption, using existing Web standards. It helps bridge the gap between human-readable Web representations and machine-actionable FAIR data.

## Features
- **Web Crawler**: Discover and follow Signposting links and other linked data patterns
- **RDF Processing**: Parse and store RDF data in various formats (Turtle, RDF/XML, JSON-LD, etc.)
- **Knowledge Graph**: Build and maintain a queryable knowledge graph of discovered resources
- **FAIR Assessment**: Evaluate resources against FAIR principles criteria
- **Visualisation**: Visualise the knowledge graph structure and connections
- **Statistics**: Generate detailed statistics about formats, link types, and discovered data
- **Export**: Export data in various RDF serialisation formats

## Installation
### Prerequisites:
- Python 3.8 or higher
- Apache Jena Fuseki (for RDF storage)
- Required Python packages (see requirements.txt)

### Setup Instructions:
1. Clone the repository: git clone https://github.com/thivi19/third-year-project.git
2. Install required packages: pip install -r requirements.txt
3. Set up Apache Jena Fuseki: Follow the instructions in the Fuseki documentation
4. Start Fuseki and create a dataset named knowledge_graph (or update the configuration to use a different dataset name)
5. Start the application: 'python app.py'
6. Open your web browser and navigate to 'http://localhost:5000'

## Usage
- Open the application in your web browser
- Enter a seed URL to start crawling
- Adjust the crawl parameters if needed
- Start the crawl and monitor progress
- Explore the results, visualisations, and statistics
- Perform FAIR assessments on discovered resources
- Export the data for further analysis

## FAIR Assessment
The application provides a detailed assessment of how well resources adhere to FAIR principles, checking for:
- **Findability**: Persistent identifiers, metadata richness, registered/indexed content
- **Accessibility**: HTTP access, content negotiation, open protocols
- **Interoperability**: Standard vocabularies, qualified references, linked data principles
- **Reusability**: Clear licensing, provenance information, community standards

## License
This project is licensed under the MIT License - see the LICENSE file for details.