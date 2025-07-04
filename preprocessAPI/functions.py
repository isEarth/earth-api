"""
경제 뉴스 자동 처리 파이프라인

이 모듈은 다음과 같은 주요 기능을 포함합니다:
- YouTube에서 자막 수집 및 전처리
- 중요 문장 필터링 및 오타 수정
- 키워드 추출 및 LDA 기반 토픽 분류
- GPT 기반 인과 문장 추출 및 문장 재구성
- 문장 임베딩 기반 관계 분석
- Neo4j에 노드/관계 저장

주요 클래스:
- CustomTokenizer: 명사 기반 형태소 토크나이저
- YoutubeScrape: 영상 자막 수집, 전처리 및 키워드 추출
- TopicSelect: LDA 모델 기반 토픽 추출
- CausalClassify: GPT 및 분류 모델을 통한 인과 문장 분류 및 재구성
- SplitSentence: 문장 분리 및 임베딩 처리
- UpdataNeo4j: 결과를 Neo4j 그래프 DB에 저장
"""

import re
from kiwipiepy import Kiwi

from gensim.models import LdaModel
from gensim.test.utils import datapath
from gensim.corpora import Dictionary
import kss
from transformers import pipeline, AutoTokenizer

import yt_dlp
import requests

from split_module.predict import *
from sklearn.feature_extraction.text import TfidfVectorizer

from neo4j import GraphDatabase
import ast
import os
from dotenv import load_dotenv
import time
from openai import OpenAI

load_dotenv()

NEO4J_URL = os.environ.get('NEO4J_URL')
NEO4J_PORT = os.environ.get('NEO4J_PORT')
NEO4J_ID = os.environ.get('NEO4J_ID')
NEO4J_PASSWORD = os.environ.get('NEO4J_PASSWORD')
OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY')
ORGANIZATION_ID = ''
PROJECT_ID = ''

class CustomTokenizer:
    """
    Kiwi 기반 사용자 정의 토크나이저.
    명사(NNG, NNP)만 추출하고, 길이가 2자 이상인 토큰만 유지함.
    """
    def __init__(self):
        self.tagger = Kiwi()

    def __call__(self, sent):
        morphs = self.tagger.analyze(sent)[0][0]  # 첫 번째 분석 결과 사용, normalize=True로 정규화
        result = [form for form, tag, _, _ in morphs if tag in ['NNG', 'NNP'] and len(form) > 1]
        return result

class YoutubeScrape:
    """
    YouTube 자막 수집 및 텍스트 전처리 유틸리티 클래스.
    """
    def get_video_text(video_url):
        """
        주어진 YouTube URL에서 자동 자막을 다운로드하고 정제된 텍스트를 반환함.
        Args:
            video_url (str): YouTube 영상 URL
        Returns:
            str: 정제된 텍스트 자막
        """
        video_id = video_url.split('v=')[1][:11]

        ydl_opts = {
            'skip_download': True,
            'writeautomaticsub': True,
            'subtitleslangs': ['ko'],
            'outtmpl': '%(id)s.%(ext)s'
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.cache.remove()
            info = ydl.extract_info(f'https://www.youtube.com/watch?v={video_id}', download=False)
            vtt_url = info.get('requested_subtitles')['ko']['url']
            subtitle = requests.get(vtt_url).text

            lines = []
            for line in subtitle.split('\n'):
                if line in lines:
                    continue
                if (
                    line.strip() == ''
                    or line.startswith('WEBVTT')
                    or line.startswith('Kind:')
                    or line.startswith('Language:')
                    or re.match(r'\d\d:\d\d:\d\d\.\d+ -->', line)
                ):
                    continue
                if re.match(r'\[.*\]', line.strip()):
                    continue
                clean = re.sub(r'<.*?>', '', line).strip()
                if clean:
                    lines.append(clean)

            text_formatted = ' '.join(lines)

        return text_formatted

    def preprocessing(text):
        """
        입력 텍스트에 대해 문장 분리, TF-IDF 기반 중요도 필터링, 오타 제거 수행.
        Returns:
            str: 전처리된 문장
        """
        text = re.sub('\n', ' ', text)
        sentences = [s for s in kss.split_sentences(text)]
        ### 중요도 낮은 문장 제거
        vectorizer = TfidfVectorizer()
        X = vectorizer.fit_transform(sentences)

        tfidf_sums = X.sum(axis=1)    # 문장별 TF-IDF 합계 (2D 행렬)
        tfidf_sums = np.array(tfidf_sums).flatten()

        threshold = np.percentile(tfidf_sums, 70)

        filtered_sentences = [
            sent.replace('안녕하세요', '').replace('[음악]', '').replace(' 네', '').replace('네 ', '').strip() for sent, score in zip(sentences, tfidf_sums) if score > threshold
        ]
        pre_text = ' '.join(filtered_sentences)

        ### 오타 교정
        kiwi = Kiwi(model_type='sbg', typos='basic_with_continual_and_lengthening')

        pattern = r'(\(.+?\))'
        pre_text = re.sub(pattern, '',pre_text)
        pattern = r'(\[.+?\])'
        pre_text = re.sub(pattern, '',pre_text)
        pattern = r'[\\/:*?"<>|.]'
        pre_text = re.sub(pattern, '',pre_text)
        pattern = r"[^\sa-zA-Z0-9ㄱ-ㅎ가-힣!\"#$%&\'()*+,-./:;<=>?@[\]^_`{|}~)※‘’·“”'͏'㈜ⓒ™©•]"
        pre_text = re.sub(pattern, '',pre_text).strip()

        tokens = kiwi.tokenize(pre_text)

        pre_text = kiwi.join(tokens)

        return pre_text

    def extract_keywords(text):
        """
        전처리된 텍스트에서 명사 키워드만 추출함.
        Returns:
            List[str]: 주요 키워드 리스트
        """
        tokenizer = CustomTokenizer()
        tokens = tokenizer(text)
        return tokens

class TopicSelect:
    """
    Gensim LDA 모델을 사용한 토픽 분류 유틸리티.
    """
    def select_topic(script):
        """
        LDA 모델로부터 주요 토픽을 예측하고, 제외 토픽을 걸러낸 후 대표 키워드 5개를 반환.
        Args:
            script (List[str]): 키워드 토큰 리스트
        Returns:
            List[str]: 대표 키워드 리스트
        """
        except_topic_id = {11, 12, 18, 23, 28}

        common_dictionary = Dictionary.load("/home/regular/workspace/Earth/earth-api/preprocessAPI/models/topic/the_2293.id2word")
        bow = common_dictionary.doc2bow(script)
        temp_file = datapath("/home/regular/workspace/Earth/earth-api/preprocessAPI/models/topic/the_2293")
        lda = LdaModel.load(temp_file)
        # 토픽 리스트 (확인용)
        topicList = lda.print_topics(num_words=5, num_topics=30)

        # LDA 모델로 토픽 분포 예측
        topic_vector = lda.get_document_topics(bow)

        # 확률 높은 순으로 정렬 후 top-N만 추출
        sorted_topics = sorted(topic_vector, key=lambda x: x[1], reverse=True)
        top_topics = sorted_topics[:6]

        for topic_id, _ in top_topics:
            if topic_id in except_topic_id:
                continue
            else:
                # 정규 표현식을 사용해 키워드만 추출 (따옴표 안의 문자열)
                keywords = re.findall(r'"([^"]*)"', topicList[topic_id][1])
                break
        return keywords[:5] # 상위 5개 키워드만 반환

class CausalClassify:
    """
    인과 문장 분류 및 요약을 수행하는 클래스.
    GPT 기반 재구성 및 HuggingFace 분류 모델 사용.
    """
    @staticmethod
    def set_open_params(
            model="gpt-4.1-nano",
            temperature=0.2,
            max_tokens=40,
            top_p=1,
            frequency_penalty=0,
            presence_penalty=0,
        ):
        """ OpenAI API 호출을 위한 파라미터 세팅 """

        openai_params = {}

        openai_params['model'] = model
        openai_params['temperature'] = temperature
        openai_params['max_tokens'] = max_tokens
        openai_params['top_p'] = top_p
        openai_params['frequency_penalty'] = frequency_penalty
        openai_params['presence_penalty'] = presence_penalty
        return openai_params

    # error, retry 추가
    @staticmethod
    def get_completion(params, system_message_content, user_prompt_content, verbose=False):
        """
        OpenAI GPT API 호출 로직. 오류 발생 시 재시도 수행.
        """
        # GPT 문장 재구성
        client = OpenAI(api_key= OPENAI_API_KEY,
                        organization=ORGANIZATION_ID,
                        project=PROJECT_ID
                    )

        messages = [
                {"role": "system", "content": system_message_content}, # 시스템 메시지는 대화의 맥락과 모델의 전반적인 행동 방식을 설정하는 데 사용
                {"role": "user", "content": user_prompt_content} ]

        retry_count = 3
        for i in range(0, retry_count):
            while True:
                try:

                    response = client.chat.completions.create(
                        model = params['model'],
                        messages = messages,
                        temperature = params['temperature'],
                        max_tokens = params['max_tokens'],
                        top_p = params['top_p'],
                        frequency_penalty = params['frequency_penalty'],
                        presence_penalty = params['presence_penalty'],
                    )

                    answer = response.choices[0].message.content
                    return answer

                except Exception as error:
                    print(f"API Error: {error}")
                    print(f"Retrying {i+1} time(s) in 4 seconds...")

                    if i+1 == retry_count:
                        return user_prompt_content, None, None
                    time.sleep(4)
                    continue
    @staticmethod
    def generate_preprocess_sentence(sentence):
        """
        문장을 핵심 명사/동사로 요약하여 10토큰 이내로 간결하게 재구성.
        GPT 모델 기반 요약 프롬프트 활용.
        """
        # --- 제약 조건 포함 프롬프트 작성 ---
        params = CausalClassify.set_open_params()

        system_message_content = """
        You are an expert at reprocessing incomplete Korean sentences into complete phrase.
        You have to summarize incomplete Korean sentences while maintaining their meaning and make them into complete phrase.
        """

        prompt_text_with_constraints =  f"""
        The tone should be similar to an economic report, analyst briefing, or business news article.
        Also, create clean sentences, leaving only the essential sentence components such as nouns, verbs, and objects.
        And write concisely and clearly, using only the words used in the given sentence.
        Leave out opinions such as predictions, possibilities, and prospects, and leave only the actions the subject has done.

        Example input:
        트럼프 대통령이 현재 시간으로 오늘 상호 관세까지 발표한다고 예고하면서

        Expected output:
        트럼프 대통령 상호 관세 발표

        The results should be printed in Korean.
        And the maximum number of tokens is 10, so it should be summarized well here.
        Do not necessarily use expressions that predict the possibility, and only clearly express actions that necessarily see the possibility.

        input:
        {sentence}

        output:
        """
        model_output = CausalClassify.get_completion(params, system_message_content, prompt_text_with_constraints, verbose=True)

        return model_output

    @staticmethod
    def inference_sentence(script):
        """
        전체 스크립트를 문장 단위로 분리하고, 인과 문장만 필터링.
        Returns:
            Tuple[List[str], List[str]]: (인과 문장, 일반 문장)
        """
        # 스크립트 전처리
        script = re.sub('\n', ' ', script)
        sentences = [s for s in kss.split_sentences(script)]

        # 모델 파이프라인 설정
        base_model_path = "./models/kf-deberta-base"
        tokenizer = AutoTokenizer.from_pretrained(base_model_path, use_fast=True)
        classify_model_path = './models/causal_detection'
        clf = pipeline(
            "text-classification",
            model=classify_model_path,
            tokenizer=tokenizer,
            device=0  # GPU 사용 시 주석 해제
        )

        # 인과 탐지
        causal_sentences = []
        general_sentencse = []
        for sen in sentences:
            output = clf(sen)[0]
            if output["label"] == "LABEL_1":
                causal_sentences.append(sen)
            else:
                general_sentencse.append(sen)

        return causal_sentences, general_sentencse

class SplitSentence:
    """
    문장 집합에 대해 CausalClassify 기반 재처리 수행 및 임베딩 추출.
    """
    def result_split(sentences):
        """
        문장을 그룹 단위로 분할하고, 각 문장에 대해 재요약 및 임베딩 생성.
        Returns:
            Tuple[List[List[str]], List[List[List[float]]]]: (분할 문장, 임베딩 리스트)
        """
        cs = split_sentences(sentences)
        result = cs.splited
        embeds = cs.embeds

        result_list = []
        for res in result:
            result_list.append([CausalClassify.generate_preprocess_sentence(s) for s in res])

        emb_list = []
        for emb in embeds:
            emb_list.append([e.tolist() for e in emb])
        return result_list, emb_list

class UpdataNeo4j:
    """
    분리된 문장 및 관계를 Neo4j에 저장하는 유틸리티 클래스.
    """
    def make_relation(split_result, emb_list):
        """
        인접 문장 간 관계 리스트 및 노드 리스트 생성.
        Returns:
            Tuple[List[Tuple[str, List[float]]], List[Tuple[str, str]]]
        """
        node_result = []
        rel_result = []
        for t_idx in range(len(split_result)):
            if len(split_result[t_idx]) == 1:
                node_result.append([split_result[t_idx][0], emb_list[t_idx][0]])
            else:
                for e_idx in range(len(split_result[t_idx])-1):
                    node_result.append([split_result[t_idx][e_idx], emb_list[t_idx][e_idx]])
                    rel_result.append([(split_result[t_idx][e_idx]), (split_result[t_idx][e_idx+1])])
                    if e_idx == len(split_result[t_idx])-2:
                        node_result.append([split_result[t_idx][e_idx+1], emb_list[t_idx][e_idx+1]])
        return node_result, rel_result

    def update_neo4j(nodes, relations, event_topics, rel_type):
        """
        노드/관계 생성 및 이벤트 연결 관계까지 포함한 Neo4j 그래프 업데이트 수행.
        Args:
            nodes: 문장 및 임베딩
            relations: 문장 간 관계
            event_topics: 연결된 주제
            rel_type: 관계 유형 (causal 또는 general)
        Returns:
            str: "ok" (성공 시)
        """
        url = f"{NEO4J_URL}:{NEO4J_PORT}"
        auth = (NEO4J_ID, NEO4J_PASSWORD)

        current_timestamp = int(time.time() * 1000)
        node_list = []
        for node in nodes:
            node_info = {
                'name': node[0],
                'embedding': node[1],
                'createdTimestamp': current_timestamp,
                'oriTopic': event_topics
            }
            node_list.append(node_info)

        with GraphDatabase.driver(url, auth=auth) as driver:
            driver.verify_connectivity()
            with driver.session(database="neo4j") as session:
                # 노드 생성
                gen_youtube_node_q = '''
                    UNWIND $node_list AS row
                    CREATE (y:Youtube {
                        name: row.name,
                        embedding: row.embedding,
                        createdTimestamp: row.createdTimestamp,
                        oriTopic: row.oriTopic
                    })
                '''
                session.run(gen_youtube_node_q, node_list=node_list)

                # 관계 생성
                if rel_type == "causal":
                    rel_type_cypher = "isCauseOf"
                else:
                    rel_type_cypher = "isGeneralOf"

                gen_youtube_rel_q = f'''
                    UNWIND $relations AS pair
                    MATCH (a:Youtube {{name: pair[0]}})
                    MATCH (b:Youtube {{name: pair[1]}})
                    CREATE (a)-[:{rel_type_cypher}]->(b)
                '''
                session.run(gen_youtube_rel_q, relations=relations)

                # 이벤트 연결 (event_topics가 리스트/문자열 형태에 따라 달라질 수 있습니다)
                # event_topics가 리스트면, 각 토픽별로 연결하거나 IN 구문 사용
                # 여기서는 간단하게 문자열(단일 토픽)로 가정
                for node in nodes:
                    topic_connect_q = '''
                        MATCH (b:Youtube {name: $name})
                        MATCH (a:Event {topics: $event_topics})
                        CREATE (a)-[:connectedYoutube]->(b)
                    '''
                    session.run(topic_connect_q, name=node[0], event_topics=event_topics)

        return "ok"