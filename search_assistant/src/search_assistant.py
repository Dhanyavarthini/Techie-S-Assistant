import json
import logging
import os
import re
import sys
from urllib.parse import urlparse

import requests
import streamlit as st
import weave
import yaml
from dotenv import load_dotenv
from langchain.chains import ConversationalRetrievalChain, RetrievalQA
from langchain.memory import ConversationSummaryMemory
from langchain.output_parsers import ResponseSchema, StructuredOutputParser
from langchain.prompts import load_prompt
from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain_community.document_loaders import AsyncHtmlLoader, UnstructuredURLLoader
from langchain_community.document_transformers import Html2TextTransformer

current_dir = os.path.dirname(os.path.abspath(__file__))
kit_dir = os.path.abspath(os.path.join(current_dir, '..'))
repo_dir = os.path.abspath(os.path.join(kit_dir, '..'))
sys.path.append(kit_dir)
sys.path.append(repo_dir)

from serpapi import GoogleSearch

from utils.model_wrappers.api_gateway import APIGateway
from utils.vectordb.vector_db import VectorDb
from utils.visual.env_utils import get_wandb_key
from official_sites import OFFICIAL_SITES
CONFIG_PATH = os.path.join(kit_dir, 'config.yaml')
PERSIST_DIRECTORY = os.path.join(kit_dir, 'data/my-vector-db')

load_dotenv(os.path.join(repo_dir, '.env'))

# Handle the WANDB_API_KEY resolution before importing weave
wandb_api_key = get_wandb_key()

# If WANDB_API_KEY is set, proceed with weave initialization
if wandb_api_key:
    import weave

    # Initialize Weave with your project name
    weave.init('sambanova_search_assistant')
else:
    print('WANDB_API_KEY is not set. Weave initialization skipped.')


class SearchAssistant:
    """
    Class used to do generation over search query results and scraped sites
    """

    def __init__(self, config=None) -> None:
        """
        Initializes the search assistant with the given configuration parameters.

        Args:
        config (dict, optional):  Extra configuration parameters for the search Assistant.
        If not provided, default values will be used.
        """

        # Set up logger
        self.logger = logging.getLogger(__name__)
        self.logger.setLevel(logging.INFO)
        handler = logging.StreamHandler()
        formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        handler.setFormatter(formatter)
        self.logger.addHandler(handler)

        if config is None:
            self.config = {}
        else:
            self.config = config
        config_info = self._get_config_info(CONFIG_PATH)
        self.api_info = config_info[0]
        self.embedding_model_info = config_info[1]
        self.llm_info = config_info[2]
        self.retrieval_info = config_info[3]
        self.web_crawling_params = config_info[4]
        self.extra_loaders = config_info[5]
        self.prod_mode = config_info[6]
        self.documents = None
        self.urls = None
        self.llm = self.init_llm_model()
        self.vectordb = VectorDb()
        self.qa_chain = None
        self.memory = None

    def _get_config_info(self, config_path):
        """
        Loads json config file

        Args:
        config_path (str): Path to the YAML configuration file.

        Returns:
        api_info (string): string containing API to use SambaStudio or SambaNovaCloud.
        embedding_model_info (string): String containing embedding model type to use, SambaStudio or CPU.
        llm_info (dict): Dictionary containing LLM parameters.
        retrieval_info (dict): Dictionary containing retrieval parameters
        web_crawling_params (dict): Dictionary containing web crawling parameters
        extra_loaders (list): list containing extra loader to use when doing web crawling (only pdf available in base kit)
        prod_mode (bool): Boolean indicating whether the app is in production mode
        """
        with open(config_path, 'r') as yaml_file:
            config = yaml.safe_load(yaml_file)
        api_info = config['api']
        embedding_model_info = config['embedding_model']
        llm_info = config['llm']
        retrieval_info = config['retrieval']
        web_crawling_params = config['web_crawling']
        extra_loaders = config['extra_loaders']
        prod_mode = config['prod_mode']

        return api_info, embedding_model_info, llm_info, retrieval_info, web_crawling_params, extra_loaders, prod_mode

    def init_memory(self):
        """
        Initialize conversation summary memory for the conversation
        """
        summary_prompt = load_prompt(os.path.join(kit_dir, 'prompts/llama3-summary.yaml'))

        self.memory = ConversationSummaryMemory(
            llm=self.llm,
            max_token_limit=100,
            buffer='The human and AI greet each other to start a conversation.',
            memory_key='chat_history',
            return_messages=True,
            output_key='answer',
            prompt=summary_prompt,
        )

    def init_llm_model(self) -> None:
        """
        Initializes the LLM endpoint

        Returns:
        llm (SambaStudio or SambaNovaCloud): Langchain LLM to use
        """
        if self.prod_mode:
            sambanova_api_key = st.session_state.SAMBANOVA_API_KEY
        else:
            if 'SAMBANOVA_API_KEY' in st.session_state:
                sambanova_api_key = os.environ.get('SAMBANOVA_API_KEY') or st.session_state.SAMBANOVA_API_KEY
            else:
                sambanova_api_key = os.environ.get('SAMBANOVA_API_KEY')

        llm = APIGateway.load_llm(
            type=self.api_info,
            streaming=True,
            coe=self.llm_info['coe'],
            do_sample=self.llm_info['do_sample'],
            max_tokens_to_generate=self.llm_info['max_tokens_to_generate'],
            temperature=self.llm_info['temperature'],
            select_expert=self.llm_info['select_expert'],
            process_prompt=False,
            sambanova_api_key=sambanova_api_key,
        )
        return llm

    def reformulate_query_with_history(self, query):
        """
        Reformulates the query based on the conversation history.

        Args:
        query (str): The current query to reformulate.

        Returns:
        str: The reformulated query.
        """
        if self.memory is None:
            self.init_memory()
        custom_condensed_question_prompt = load_prompt(
            os.path.join(kit_dir, 'prompts', 'llama3-multiturn-custom_condensed_question.yaml')
        )
        history = self.memory.load_memory_variables({})
        reformulated_query = self.llm.invoke(
            custom_condensed_question_prompt.format(chat_history=history, question=query)
        )
        self.logger.info(f"Original query: {query}")
        self.logger.info(f"Reformulated query: {reformulated_query}")
        return reformulated_query

    def remove_links(self, text):
        """
        Removes all URLs from the given text.

        Args:
        text (str): The text from which to remove URLs.

        Returns:
        str: The text with all URLs removed.
        """
        url_pattern = r'https?://\S+|www\.\S+'
        return re.sub(url_pattern, '', text)

    import re

    def parse_serp_analysis_output(self, answer, links):
        """
        Parse the output of the SERP analysis prompt to replace the reference numbers with HTML links.

        Parameters:
        answer (str): The LLM-generated answer using the SERP tool output.
        links (list): A list of links corresponding to the reference numbers in the prompt.

        Returns:
        str: The parsed output with HTML links instead of reference numbers.
        """

        def replace_reference(match):
            # Extract the reference number
            ref_num = int(match.group(1))
            # Check if the reference number has a corresponding link
            if ref_num <= len(links):
                return f'[<sup>{ref_num}</sup>]({links[ref_num - 1]})'
            else:
                return f'[<sup>{ref_num}</sup>](#)'  # Placeholder if no link available

        # Regular expression to match variations of [reference:n] or [Reference: n]
        pattern = re.compile(r'\[reference: ?(\d+)\]|\[Reference: ?(\d+)\]', re.IGNORECASE)

        # Replace all occurrences in the answer with corresponding links
        parsed_answer = pattern.sub(lambda m: replace_reference(m), answer)

        return parsed_answer

    def restrict_to_official_sites(self, query: str, official_sites: list) -> str:
        site_restrictions = " OR ".join([f"site:{site}" for site in official_sites])
        return f"{query} ({site_restrictions})"

    def querySerpapi(
        self,
        query: str,
        limit: int = 1,
        do_analysis: bool = True,
        engine='google',
    ) -> str:
        """
        A search engine using Serpapi API. Useful for when you need to answer questions about current events. Input should be a search query.

        Parameters:
        query (str): The query to search.
        limit (int, optional): The maximum number of search results to retrieve. Defaults to 5.
        do_analysis (bool, optional): Whether to perform the LLM analysis directly on the search results. Defaults to True.
        engine (str, optional): The search engine to use

        Returns:
        tuple: A tuple containing the search results or parsed llm generation and the corresponding links.
        """
        restricted_query = self.restrict_to_official_sites(query, OFFICIAL_SITES)
        if engine not in ['google', 'bing']:
            raise ValueError('engine must be either google or bing')
        params = {
            'q': restricted_query,
            'num': limit,
            'engine': engine,
            'api_key': st.session_state.SERPAPI_API_KEY if self.prod_mode else os.environ.get('SERPAPI_API_KEY'),
        }

        try:
            search = GoogleSearch(params)
            response = search.get_dict()

            knowledge_graph = response.get('knowledge_graph', None)
            results = response.get('organic_results', [])

            links = []
            if len(results) > 0:
                links = [r['link'] for r in results]
                context = []
                for i, result in enumerate(results):
                    context.append(f'[reference:{i+1}] {result.get("title", "")}: {result.get("snippet", "")}')
                context = '\n\n'.join(context)
                self.logger.info(f'Context found: {context}')
            else:
                context = 'Answer not found'
                links = []
                self.logger.info(f'No answer found for query: {query}. Raw response: {response}')
        except Exception as e:
            context = 'Answer not found'
            links = []
            self.logger.error(f'Error message: {e}')

        if do_analysis:
            prompt = load_prompt(os.path.join(kit_dir, 'prompts/llama3-serp_analysis.yaml'))
            formatted_prompt = prompt.format(question=query, context=context)
            answer = self.llm.invoke(formatted_prompt)
            return self.parse_serp_analysis_output(answer, links), links
        else:
            return context, links

    def load_remote_pdf(self, url):
        """
        Load PDF files from the given URL.
        Args:
            url (str): URL to load pdf document from.
        Returns:
            list: A list of loaded pdf documents.
        """
        loader = UnstructuredURLLoader(urls=[url])
        docs = loader.load()
        return docs

    def load_htmls(self, urls, extra_loaders=None):
        """
        Load HTML documents from the given URLs.
        Args:
            urls (list): A list of URLs to load HTML documents from.
        Returns:
            list: A list of loaded HTML documents.
        """
        if extra_loaders is None:
            extra_loaders = []
        docs = []
        for url in urls:
            if url.endswith('.pdf'):
                if 'pdf' in extra_loaders:
                    docs.extend(self.load_remote_pdf(url))
                else:
                    continue
            else:
                loader = AsyncHtmlLoader(url, verify_ssl=False)
                docs.extend(loader.load())
        return docs

    def link_filter(self, all_links, excluded_links):
        """
        Filters a list of links based on a list of excluded links.
        Args:
            all_links (List[str]): A list of links to filter.
            excluded_links (List[str]): A list of excluded links.
        Returns:
            Set[str]: A list of filtered links.
        """
        clean_excluded_links = set()
        for excluded_link in excluded_links:
            parsed_link = urlparse(excluded_link)
            clean_excluded_links.add(parsed_link.netloc + parsed_link.path)
        filtered_links = set()
        for link in all_links:
            # Check if the link contains any of the excluded links
            if not any(excluded_link in link for excluded_link in clean_excluded_links):
                filtered_links.add(link)
        return filtered_links

    def clean_docs(self, docs):
        """
        Clean the given HTML documents by transforming them into plain text.
        Args:
            docs (list): A list of langchain documents with html content to clean.
        Returns:
            list: A list of cleaned plain text documents.
        """
        html2text_transformer = Html2TextTransformer()
        docs = html2text_transformer.transform_documents(documents=docs)
        return docs

    def web_crawl(self, urls, excluded_links=None):
        """
        Perform web crawling, retrieve and clean HTML documents from the given URLs, with specified depth of exploration.
        Args:
            urls (list): A list of URLs to crawl.
            excluded_links (list, optional): A list of links to exclude from crawling. Defaults to None.
            depth (int, optional): The depth of crawling, determining how many layers of internal links to explore. Defaults to 1
        Returns:
            tuple: A tuple containing the langchain documents (list) and the scrapped URLs (list).
        """
        if excluded_links is None:
            excluded_links = []
        excluded_links.extend(self.web_crawling_params['excluded_links'])
        excluded_link_suffixes = {'.ico', '.svg', '.jpg', '.png', '.jpeg', '.', '.docx', '.xls', '.xlsx'}
        scrapped_urls = []

        urls = [url for url in urls if not url.endswith(tuple(excluded_link_suffixes))]
        urls = self.link_filter(urls, set(excluded_links))
        print(f'{urls=}')
        if len(urls) == 0:
            raise ValueError(
                'not sites to scrape after filtering links, check the excluded_links config or increase Max number of results to retrieve'
            )
        urls = list(urls)[: self.web_crawling_params['max_scraped_websites']]

        scraped_docs = self.load_htmls(urls, self.extra_loaders)
        scrapped_urls.extend(urls)

        docs = self.clean_docs(scraped_docs)
        self.documents = docs
        self.urls = scrapped_urls

    def get_text_chunks_with_references(self, docs: list, chunk_size: int, chunk_overlap: int) -> list:
        """
        Splits documents into chunks and appends references to the chunked text if metadata exists.

        Args:
            docs (list): List of documents or texts. If metadata is not passed, this parameter is a list of documents.
                         If metadata is passed, this parameter is a list of texts.
            chunk_size (int): Chunk size in number of characters.
            chunk_overlap (int): Chunk overlap in number of characters.

        Returns:
            list: List of documents with text chunks, each having references appended.
        """

        text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=chunk_size, chunk_overlap=chunk_overlap, length_function=len
        )

        # Map self.urls (assumed to be a list of URLs or sources) to reference indices
        sources = {site: i + 1 for i, site in enumerate(self.urls)}

        # Split documents into chunks
        chunks = text_splitter.split_documents(docs)

        # Add references to each chunk
        for chunk in chunks:
            # Check if the chunk has metadata and if 'source' exists
            if 'metadata' in chunk and 'source' in chunk.metadata:
                reference = chunk.metadata['source']
                # Verify if the reference exists in the sources
                if reference in sources:
                    chunk.page_content = f'[reference:{sources[reference]}] {chunk.page_content}\n\n'
                else:
                    chunk.page_content = f'[reference:unknown] {chunk.page_content}\n\n'
            else:
                # Handle cases where metadata or source is missing
                chunk.page_content = f'[reference:unknown] {chunk.page_content}\n\n'

        return chunks

    def create_load_vector_store(self, force_reload: bool = False, update: bool = False):
        """
        Create or load a vector store based on the given documents.
        Args:
            force_reload (bool, optional): Whether to force reloading the vector store. Defaults to False.
            update (bool, optional): Whether to update the vector store. Defaults to False.
        """

        persist_directory = self.config.get('persist_directory', 'NoneDirectory')

        # Load the embeddings model
        embeddings = APIGateway.load_embedding_model(
            type=self.embedding_model_info['type'],
            batch_size=self.embedding_model_info['batch_size'],
            coe=self.embedding_model_info['coe'],
            select_expert=self.embedding_model_info['select_expert'],
        )

        # If directory exists and no force reload or update is required
        if os.path.exists(persist_directory) and not force_reload and not update:
            self.vector_store = self.vectordb.load_vdb(
                persist_directory, embeddings, db_type=self.retrieval_info['db_type']
            )

        # If update is required, reload the chunks and update the vector store
        elif os.path.exists(persist_directory) and update:
            chunks_with_references = self.get_text_chunks_with_references(
                self.documents, self.retrieval_info['chunk_size'], self.retrieval_info['chunk_overlap']
            )
            # Include both text chunks and references in vector store
            self.vector_store = self.vectordb.load_vdb(
                persist_directory, embeddings, db_type=self.retrieval_info['db_type']
            )
            self.vector_store = self.vectordb.update_vdb(
                chunks_with_references, embeddings, self.retrieval_info['db_type'], persist_directory
            )

        # If no vector store exists, create a new one with references
        else:
            chunks_with_references = self.get_text_chunks_with_references(
                self.documents, self.retrieval_info['chunk_size'], self.retrieval_info['chunk_overlap']
            )
            # Create vector store with both text chunks and references
            self.vector_store = self.vectordb.create_vector_store(
                chunks_with_references, embeddings, self.retrieval_info['db_type'], persist_directory
            )

    def create_and_save_local(self, input_directory=None, persist_directory=None, update=False):
        """
        Create and save a vector store based on the given documents.
        Args:
            input_directory: The directory containing the previously created vector store.
            persist_directory: The directory to save the vector store.
            update (bool, optional): Whether to update the vector store. Defaults to False.
        """

        persist_directory = persist_directory or self.config.get('persist_directory', 'NoneDirectory')

        # Get text chunks along with references
        chunks_with_references = self.get_text_chunks_with_references(
            self.documents, self.retrieval_info['chunk_size'], self.retrieval_info['chunk_overlap']
        )

        # Load the embeddings model
        embeddings = APIGateway.load_embedding_model(
            type=self.embedding_model_info['type'],
            batch_size=self.embedding_model_info['batch_size'],
            coe=self.embedding_model_info['coe'],
            select_expert=self.embedding_model_info['select_expert'],
        )

        # If updating an existing vector store
        if update and os.path.exists(persist_directory):
            self.config['update'] = True
            self.vector_store = self.vectordb.update_vdb(
                chunks_with_references, embeddings, self.retrieval_info['db_type'], input_directory, persist_directory
            )

        # If creating a new vector store
        else:
            if os.path.exists(persist_directory):
                self.vector_store = self.vectordb.create_vector_store(
                    chunks_with_references, embeddings, self.retrieval_info['db_type'], persist_directory
                )
            else:
                self.vector_store = self.vectordb.create_vector_store(
                    chunks_with_references, embeddings, self.retrieval_info['db_type'], None
                )

    def basic_call(
        self,
        query,
        reformulated_query=None,
        search_method='serpapi',
        max_results=5,
        search_engine='google',
        conversational=False,
    ):
        """
        Do a basic call to the llm using the query result snippets as context
        Args:
            query (str): The query to search.
            reformulated_query (str, optional): The reformulated query to search. Defaults to None.
            search_method (str, optional): The search method to use. Defaults to "serpapi".
            max_results (int, optional): The maximum number of search results to retrieve. Defaults to 5.
            search_engine (str, optional): The search engine to use. Defaults to "google".
            conversational (bool, optional): Whether to save conversation to memory. Defaults to False.
        """
        if reformulated_query is None:
            reformulated_query = query

        if search_method == 'serpapi':
            answer, links = self.querySerpapi(
                query=reformulated_query, limit=max_results, engine=search_engine, do_analysis=True
            )
        elif search_method == 'serper':
            answer, links = self.querySerper(query=reformulated_query, limit=max_results, do_analysis=True)
        elif search_method == 'openserp':
            answer, links = self.queryOpenSerp(
                query=reformulated_query, limit=max_results, engine=search_engine, do_analysis=True
            )

        if conversational:
            self.memory.save_context(inputs={'input': query}, outputs={'answer': answer})

        return {'answer': answer, 'sources': links}

    def set_retrieval_qa_chain(self, conversational=False):
        """
        Set a retrieval chain for queries that use as retriever a previously created vectorstore
        """
        retrieval_qa_prompt = load_prompt(os.path.join(kit_dir, 'prompts/llama3-web_scraped_data_retriever.yaml'))
        retriever = self.vector_store.as_retriever(
            search_type='similarity_score_threshold',
            search_kwargs={
                'score_threshold': self.retrieval_info['score_treshold'],
                'k': self.retrieval_info['k_retrieved_documents'],
            },
        )
        if conversational:
            self.init_memory()

            custom_condensed_question_prompt = load_prompt(
                os.path.join(kit_dir, 'prompts', 'llama3-multiturn-custom_condensed_question.yaml')
            )

            self.qa_chain = ConversationalRetrievalChain.from_llm(
                llm=self.llm,
                retriever=retriever,
                memory=self.memory,
                chain_type='stuff',
                return_source_documents=True,
                verbose=False,
                condense_question_prompt=custom_condensed_question_prompt,
                combine_docs_chain_kwargs={'prompt': retrieval_qa_prompt},
            )

        else:
            self.qa_chain = RetrievalQA.from_llm(
                llm=self.llm,
                retriever=retriever,
                return_source_documents=True,
                verbose=False,
                input_key='question',
                output_key='answer',
                prompt=retrieval_qa_prompt,
            )

    def search_and_scrape(self, query, search_method='serpapi', max_results=5, search_engine='google'):
        """
        Do a call to the serp tool, scrape the url results, and save the scraped data in a a vectorstore
        Args:
            query (str): The query to search.
            max_results (int): The maximum number of search results. Default is 5
            search_method (str, optional): The search method to use. Defaults to "serpapi".
            search_engine (str, optional): The search engine to use. Defaults to "google".
        """
        if search_method == 'serpapi':
            _, links = self.querySerpapi(query=query, limit=max_results, engine=search_engine, do_analysis=False)
        elif search_method == 'serper':
            _, links = self.querySerper(query=query, limit=max_results, do_analysis=False)
        elif search_method == 'openserp':
            _, links = self.queryOpenSerp(query=query, limit=max_results, engine=search_engine, do_analysis=False)
        if len(links) > 0:
            self.web_crawl(urls=links)
            # self.create_load_vector_store()
            self.create_and_save_local()
            self.set_retrieval_qa_chain(conversational=True)
        else:
            return {'message': f"No links found for '{query}'. Try again"}

    def get_relevant_queries(self, query):
        """
        Generates a list of related queries based on the input query.

        Args:
        query (str): The input query for which related queries are to be generated.

        Returns:
        list: A list of related queries based on the input query.
        """
        prompt = load_prompt(os.path.join(kit_dir, 'prompts/llama3-related_questions.yaml'))
        response_schemas = [ResponseSchema(name='related_queries', description=f'related search queries', type='list')]
        list_output_parser = StructuredOutputParser.from_response_schemas(response_schemas)
        list_format_instructions = list_output_parser.get_format_instructions()
        relevant_queries_chain = prompt | self.llm | list_output_parser
        input_variables = {'question': query, 'format_instructions': list_format_instructions}
        return relevant_queries_chain.invoke(input_variables).get('related_queries', [])

    def parse_retrieval_output(self, result):
        """
        Parses the output of the retrieval chain to map the original source numbers with the numbers in generation.

        Args:
        result (dict): The result from the retrieval chain, containing the answer and source documents.

        Returns:
        str: The parsed answer with the mapped source numbers.
        """
        parsed_answer = self.parse_serp_analysis_output(result['answer'], self.urls)
        # mapping original sources order with question used sources order
        question_sources = set(f'{doc.metadata["source"]}' for doc in result['source_documents'])
        question_sources_map = {source: i + 1 for i, source in enumerate(question_sources)}
        for i, link in enumerate(self.urls):
            if link in parsed_answer:
                parsed_answer = parsed_answer.replace(
                    f'[<sup>{i+1}</sup>]({link})', f'[<sup>{question_sources_map[link]}</sup>]({link})'
                )
        return parsed_answer

    def retrieval_call(self, query):
        """
        Do a call to the retriever chain

        Args:
        query (str): The query to search.

        Returns:
        result (str): The final Result to the user query
        """
        result = self.qa_chain.invoke(query)
        result['answer'] = self.parse_retrieval_output(result)
        return result