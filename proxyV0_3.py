#!/usr/bin/env python
# -*- coding: utf-8 -*-
# 源码的编码形式是utf-8
# 另外，鉴于编码在本程序中有非常重要的作用，弄清楚python中编码的概念是很有必要的
# 这篇文章讲得比较清楚： http://blog.csdn.net/lxdcyh/article/details/4018054
"""
    proxy alpha V0.3版
    2017/12/13
    Bryce Chen
    chenjay@sjtu.edu.cn
    
    类：
        代理中心        Proxy ： 统筹代理各方面组件交互
        客户端/服务端   Client/Server (继承自 连接Connection) ： 对浏览器、服务器连接的抽象，封装数据传输行为
        网页分析器      HttpParser ： 分析得到请求/响应的各个字段    
        认证中心        Authenticator  : 统筹控制扩展功能（敏感词屏蔽、上网记录）
        数据库          DataBase       : 提供上网日志、敏感词列表、控制规则、系统配置的存储
    
    已完成功能:
        敏感词过滤   : 替换敏感词为指定词汇 （注意点：gzip压缩过的网页要先取出body解压、替换后重新压缩content-length也要修改）
        上网日志     ： 记录上网时间、用户IP、网址、请求文件及类型、附带参数、Cookie
        上网管控（黑名单模式）： 根据黑名单规则，对用户的上网行为进行管控
        上网管控（白名单模式）： 用户只能在白名单规则下上网
        
        
"""
# 该段没有实质内容
VERSION = (0, 2)
__version__ = '.'.join(map(str, VERSION[0:2]))
__description__ = 'HTTP Proxy Server in Python'
__author__ = 'Abhinav Singh'
__author_email__ = 'mailsforabhinav@gmail.com'
__homepage__ = 'https://github.com/abhinavsingh/proxy.py'
__license__ = 'BSD'

# 系统库、多进程、时间、命令行参数读取、日志、socket、线程池管理
import sys
import multiprocessing
import datetime
import time
import argparse
import logging
import socket
import select

# 用于解决网页被 gzip格式压缩过的问题，从而实现内容过滤
from gzip import GzipFile
import gzip
from StringIO import StringIO

# 这个库是我加进去追踪函数，生成函数调用图的
#from pycallgraph import PyCallGraph
#from pycallgraph.output import GraphvizOutput

# 数据库接口，用于和数据库交互
import sqlite3

logger = logging.getLogger(__name__)

# 这里区别Pyhotn3还是2，目的是统一处理：
# 用变量text_type、binary_type来存储类型。代码整洁
PY3 = sys.version_info[0] == 3

if PY3:
    text_type = str
    binary_type = bytes
    from urllib import parse as urlparse
else:
    text_type = unicode
    binary_type = str
    import urlparse


# 该函数得到text抽象类型：
# 判断如果s是binary_type类型则按照utf-8解码为Unicode后返回，否则直接返回s
# 知识点1（源码是utf-8编码，所以函数头默认的encoding是utf-8，当然也可以自己传参数）
# 知识点2（解码的结果就是Unicode，因为系统内部字符串就是unicode）
def text_(s, encoding='utf-8', errors='strict'):
    if isinstance(s, binary_type):
        return s.decode(encoding, errors)  # 这里是decode,也就是从utf-8到Unicode
    return s  # pragma: no cover


# 该函数得到binary抽象类型：
# 在此判断如果s是text_type类型则把它编码为utf-8后返回，否则直接返回s
def bytes_(s, encoding='utf-8', errors='strict'):
    if isinstance(s, text_type):  # pragma: no cover
        return s.encode(encoding, errors)  # 注意这里是encode，从Unicode到utf-8
    return s


version = bytes_(__version__)

# 这里对几个符号用变量表示，整洁代码：
# 知识点3:
# py3 str == py2 unicode
# py3 bytes == py2 str == b''前缀字符串（因此py2里,b前缀没什么具体意义，更多地是为了代码兼容运行在py3的写法）
CRLF, COLON, SP = b'\r\n', b':', b' '

# 对HttpParser初始化时，定义这是一个请求分析器还是响应分析器：
HTTP_REQUEST_PARSER = 1
HTTP_RESPONSE_PARSER = 2

# HttpParser类用于控制整个http传输
# 知识点4：HTTP请求的4部分:请求行、请求头部、空行和请求数据
# ＜request-line＞                    首行为 请求方式(GET、POST、HEAD等)
# ＜headers＞                         头部为User-Agent、Accept客户端可接收内容类型列表等
# ＜blank line i.e CRLF＞             空行（即headers和body的分隔标志）
# [＜request-body＞]                  报文内容

# 知识点5：HTTP响应的4部分:状态行、响应头部、空行和响应正文
# <response-line>                    首行为为状态码（200、404、500等） eg.HTTP/1.1 200 OK
# ＜headers＞                         消息报头为 Content-Length正文长度、Content-Type正文内容的类型等
# ＜blank line i.e CRLF＞             空行（即headers和body的分隔标志）
# [＜request-body＞]/<response-body>  报文内容
HTTP_PARSER_STATE_INITIALIZED = 1
HTTP_PARSER_STATE_LINE_RCVD = 2  # HttpParser已接收首行
HTTP_PARSER_STATE_RCVING_HEADERS = 3  # HttpParser正接收头部（还没遇到空行）
HTTP_PARSER_STATE_HEADERS_COMPLETE = 4  # HttpParser已接收头部
HTTP_PARSER_STATE_RCVING_BODY = 5  # HttpParser正接收内容
HTTP_PARSER_STATE_COMPLETE = 6  # HttpParser已接收完成

CHUNK_PARSER_STATE_WAITING_FOR_SIZE = 1
CHUNK_PARSER_STATE_WAITING_FOR_DATA = 2
CHUNK_PARSER_STATE_COMPLETE = 3


class ChunkParser(object):
    """HTTP chunked encoding response parser."""

    def __init__(self):
        self.state = CHUNK_PARSER_STATE_WAITING_FOR_SIZE
        self.body = b''
        self.chunk = b''
        self.size = None

    def parse(self, data):
        more = True if len(data) > 0 else False
        while more: more, data = self.process(data)

    def process(self, data):
        if self.state == CHUNK_PARSER_STATE_WAITING_FOR_SIZE:
            line, data = HttpParser.split(data)
            self.size = int(line, 16)
            self.state = CHUNK_PARSER_STATE_WAITING_FOR_DATA
        elif self.state == CHUNK_PARSER_STATE_WAITING_FOR_DATA:
            remaining = self.size - len(self.chunk)
            self.chunk += data[:remaining]
            data = data[remaining:]
            if len(self.chunk) == self.size:
                data = data[len(CRLF):]
                self.body += self.chunk
                if self.size == 0:
                    self.state = CHUNK_PARSER_STATE_COMPLETE
                else:
                    self.state = CHUNK_PARSER_STATE_WAITING_FOR_SIZE
                self.chunk = b''
                self.size = None
        return len(data) > 0, data


class HttpParser(object):
    """整个Http请求/响应的解析器"""

    def __init__(self, type=None):
        self.state = HTTP_PARSER_STATE_INITIALIZED
        self.type = type if type else HTTP_REQUEST_PARSER  # 定义报文类型，默认为请求类型

        self.raw = b''  # 累积的原始的报文数据
        self.new = b''  # 敏感词过滤的报文数据
        self.buffer = b''  # parser的缓冲区

        self.headers = dict()  # 知识点6：头部是键值对形式的列表，适合用dict存储
        self.body = None  # 内容体

        self.method = None  # 知识点7：请求头的方法,GET、POST、HEAD等
        self.url = None  # 解析得到url,后续可以针对url进行控制
        self.code = None  # 只是点8：响应头的代码 2XX成功、3XXURL被移走、4XX客户错误、5XX服务器错误
        self.reason = None  # 返回的缘由
        self.version = None

        self.chunker = None

    # 循环调用process()解析data
    def parse(self, data):
        self.raw += data  # 累积原始报文
        data = self.buffer + data  # 拼接buffer和data，再暂存在data中
        self.buffer = b''  # 清空buffer

        more = True if len(data) > 0 else False
        while more:
            more, data = self.process(data)
        self.buffer = data

    # 解析data的状态控制器
    # 如果已解析头部，且是（POST请求或在解析响应）：
    # -- 则进入body接收状态，把data放入body。同时如果body的长度>=头部中的长度信息，则状态进入已完成
    # 分离出首行
    # 如果未接收首行：
    # -- 则调用process_line()
    # 如果未解析头部：
    # -- 则调用process_header()
    # 如果正好解析完头部，且是Request分析，且不是POST 且 当前报文数据由两个CRLF结尾
    def process(self, data):
        if self.state >= HTTP_PARSER_STATE_HEADERS_COMPLETE and \
                (self.method == b"POST" or self.type == HTTP_RESPONSE_PARSER):
            if not self.body:
                self.body = b''

            if b'content-length' in self.headers:
                self.state = HTTP_PARSER_STATE_RCVING_BODY
                self.body += data
                if len(self.body) >= int(self.headers[b'content-length'][1]):
                    self.state = HTTP_PARSER_STATE_COMPLETE
            elif b'transfer-encoding' in self.headers and self.headers[b'transfer-encoding'][1].lower() == b'chunked':
                if not self.chunker:
                    self.chunker = ChunkParser()
                self.chunker.parse(data)
                if self.chunker.state == CHUNK_PARSER_STATE_COMPLETE:
                    self.body = self.chunker.body
                    self.state = HTTP_PARSER_STATE_COMPLETE

            return False, b''

        line, data = HttpParser.split(data)
        if line == False: return line, data

        if self.state < HTTP_PARSER_STATE_LINE_RCVD:
            self.process_line(line)
        elif self.state < HTTP_PARSER_STATE_HEADERS_COMPLETE:
            self.process_header(line)

        if self.state == HTTP_PARSER_STATE_HEADERS_COMPLETE and \
                        self.type == HTTP_REQUEST_PARSER and \
                not self.method == b"POST" and \
                self.raw.endswith(CRLF * 2):
            self.state = HTTP_PARSER_STATE_COMPLETE

        return len(data) > 0, data

    # 判断一下是Request还是Response
    # 在对应位置解析出请求方法、url、版本 (注意这里url使用了urlparse库来解析)
    # 在对应位置解析出版本、响应代号、原因
    def process_line(self, data):
        line = data.split(SP)
        if self.type == HTTP_REQUEST_PARSER:
            self.method = line[0].upper()
            self.url = urlparse.urlsplit(line[1])
            self.version = line[2]
        else:
            self.version = line[0]
            self.code = line[1]
            self.reason = b' '.join(line[2:])
        self.state = HTTP_PARSER_STATE_LINE_RCVD

    # 辅助状态控制
    # 根据冒号分割，解析得到header，然后放到headers字典里面
    def process_header(self, data):
        if len(data) == 0:
            if self.state == HTTP_PARSER_STATE_RCVING_HEADERS:
                self.state = HTTP_PARSER_STATE_HEADERS_COMPLETE
            elif self.state == HTTP_PARSER_STATE_LINE_RCVD:
                self.state = HTTP_PARSER_STATE_RCVING_HEADERS
        else:
            self.state = HTTP_PARSER_STATE_RCVING_HEADERS
            parts = data.split(COLON)
            key = parts[0].strip()
            value = COLON.join(parts[1:]).strip()
            self.headers[key.lower()] = (key, value)

    def build_url(self):
        if not self.url:
            return b'/None'

        url = self.url.path
        if url == b'': url = b'/'
        if not self.url.query == b'': url += b'?' + self.url.query
        if not self.url.fragment == b'': url += b'#' + self.url.fragment
        return url

    def build_header(self, k, v):
        return k + b": " + v + CRLF

    def build(self, del_headers=None, add_headers=None):
        req = b" ".join([self.method, self.build_url(), self.version])
        req += CRLF

        if not del_headers: del_headers = []
        for k in self.headers:
            if not k in del_headers:
                req += self.build_header(self.headers[k][0], self.headers[k][1])

        if not add_headers: add_headers = []
        for k in add_headers:
            req += self.build_header(k[0], k[1])

        req += CRLF
        if self.body:
            req += self.body

        return req

    @staticmethod
    def split(data):
        pos = data.find(CRLF)
        if pos == -1: return False, data
        line = data[:pos]
        data = data[pos + len(CRLF):]
        return line, data


class Connection(object):
    """服务器/客户端的TCP连接的封装
        本质上是socket"""

    # 需要接受一个参数what，决定连接类型(面向浏览器还是面向服务器)
    # 初始化，申明了缓冲区、连接状态、连接类型（S/C）
    def __init__(self, what):
        self.buffer = b''
        self.closed = False
        self.what = what  # server or client

    # 包装socket的send
    def send(self, data):
        return self.conn.send(data)

    # 包装socket的recv，并做了日志记录
    def recv(self, bytes=8192):
        try:
            data = self.conn.recv(bytes)
            if len(data) == 0:
                logger.debug('recvd 0 bytes from %s' % self.what)
                return None
            logger.debug('rcvd %d bytes from %s' % (len(data), self.what))
            return data
        except Exception as e:
            logger.exception(
                'Exception while receiving from connection %s %r with reason %r' % (self.what, self.conn, e))
            return None

    # 关闭连接并修改连接状态
    def close(self):
        self.conn.close()
        self.closed = True

    # 返回当前缓冲区的长度（字节）
    def buffer_size(self):
        return len(self.buffer)

    # 判断缓冲区内是否有数据
    def has_buffer(self):
        return self.buffer_size() > 0

    # 向缓冲区内填充数据data
    def queue(self, data):
        self.buffer += data

    # 从缓冲区取出数据发送，并移动队列（可能一次性发不完）
    def flush(self):
        sent = self.send(self.buffer)
        self.buffer = self.buffer[sent:]
        logger.debug('flushed %d bytes to %s' % (sent, self.what))


class Server(Connection):
    """建立面向服务器的TCP连接"""

    # 初始化接受两个参数，即服务器的地址和端口（在另一个函数中连接至服务器）
    def __init__(self, host, port):
        super(Server, self).__init__(b'server')
        self.addr = (host, int(port))

    # 建立面向服务器的连接
    def connect(self):
        self.conn = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.conn.connect((self.addr[0], self.addr[1]))


class Client(Connection):
    """从HTTP对象获得来自浏览器的TCP连接"""

    # 初始化接受两个参数，一个是socket对象，另一个是地址的字符串描述
    def __init__(self, conn, addr):
        super(Client, self).__init__(b'client')
        self.conn = conn
        self.addr = addr


class ProxyError(Exception):
    pass


class ProxyConnectionFailed(ProxyError):
    def __init__(self, host, port, reason):
        self.host = host
        self.port = port
        self.reason = reason

    def __str__(self):
        return '<ProxyConnectionFailed - %s:%s - %s>' % (self.host, self.port, self.reason)


class Proxy(multiprocessing.Process):
    """HTTP proxy implementation.
    Accepts connection object and act as a proxy between client and server.
    """

    # 知识点11：报文内行的分隔符其实就是CRLF
    def __init__(self, client):
        super(Proxy, self).__init__()

        self.start_time = self._now()  # start_time/last_activity 记录启动时间、该连接的最后活跃时间
        self.last_activity = self.start_time

        self.client = client  # client/server 继承自Connection的Client/Server对象
        self.server = None

        self.request = HttpParser()  # request/response 用于分析Http报文的HttpParser对象
        self.response = HttpParser(HTTP_RESPONSE_PARSER)

        self.grant = True  # grant 用于标识当前请求是否得到授权（扩展控制）
        # connection_established_pkt 成功连接代理的包（将返回给浏览器）
        self.connection_established_pkt = CRLF.join([
            b'HTTP/1.1 200 Connection established',
            b'Proxy-agent: proxy.py v' + version,
            CRLF
        ])

        # request_rejected_pkt 请求驳回的包
        self.request_rejected_pkt = CRLF.join([
            b'HTTP/1.1 404 Not Found',
            b'Proxy-agent: proxy.py v' + version,
            b'Content-Length: 58',
            b'Connection: close',
            CRLF,
        ]) + CRLF.join([
            b'Request Rejected By Proxy:',
            b'this attempt has been recorded'])

    # 以下3个函数返回当前时间、返回不活跃时间、返回是否判定为不活跃状态
    # 超过30s没有发生数据传输则认为是不活跃
    def _now(self):
        return datetime.datetime.utcnow()

    def _inactive_for(self):
        return (self._now() - self.last_activity).seconds

    def _is_inactive(self):
        return self._inactive_for() > 30

    # 如果已经有了server且server连接未关闭:
    # -- 调用server.queue直接放data到server.buffer中，然后返回（不分析内容）
    # 如果还没有server 或者server连接关闭：
    # -- 调用request.parse分析data
    # -- parse返回后，如果request的状态是已完成，则对分析得到的url进行连接，并且得到server对象（这里是第一次得到server）
    def _process_request(self, data):
        # once we have connection to the server
        # we don't parse the http request packets
        # any further, instead just pipe incoming
        # data from client to server
        if self.server and not self.server.closed:
            self.server.queue(data)
            return

        # parse http request
        self.request.parse(data)

        # once http request parser has reached the state complete
        # we attempt to establish connection to destination server
        if self.request.state == HTTP_PARSER_STATE_COMPLETE:
            logger.debug('request parser is in state complete')

            # 一旦request已经解析完成，则进入授权审查阶段
            # 初始化Authenticator，调用grant接口
            self.auth = Authenticator()
            if not self.auth.grant(self.request, self.client):
                self.client.queue(self.request_rejected_pkt)
                return

            if self.request.method == b"CONNECT":
                host, port = self.request.url.path.split(COLON)
            elif self.request.url:
                host, port = self.request.url.hostname, self.request.url.port if self.request.url.port else 80

            self.server = Server(host, port)
            try:
                logger.debug('connecting to server %s:%s' % (host, port))
                self.server.connect()
                logger.debug('connected to server %s:%s' % (host, port))
            except Exception as e:
                self.server.closed = True
                raise ProxyConnectionFailed(host, port, repr(e))

            # for http connect methods (https requests)
            # queue appropriate response for client
            # notifying about established connection
            if self.request.method == b"CONNECT":
                self.client.queue(self.connection_established_pkt)
            # for usual http requests, re-build request packet
            # and queue for the server with appropriate headers
            else:
                self.server.queue(self.request.build(
                    del_headers=[b'proxy-connection', b'connection', b'keep-alive'],
                    add_headers=[(b'Connection', b'Close')]
                ))

    # 调用respose.parse解析data内容
    # 调用client.queue把data放到buffer中
    def _process_response(self, data):
        # parse incoming response packet
        # only for non-https requests
        if not self.request.method == b"CONNECT":
            self.response.parse(data)
        # queue data for client

        # 如果当前response头部中指明为文本类型:
        # -- 等待response获得完整的body，有了完整的HTTP报文存储在response当中
        # -- 判断是否开启了敏感词过滤模式：
        # -- -- 调用敏感词过滤函数（通过gzip解压缩body，得到原文内容，替换后重新压缩）
        # -- 不管是否过滤，已经完成分析的html报文都要发送至浏览器
        # 如果是图片等其他格式：
        # 直接向浏览器传输分段数据（也包括图片的http报文发送回浏览器）

        if self.response.headers.has_key('content-type') and \
                        'text/html' in self.response.headers['content-type'][1]:
            if self.response.state == HTTP_PARSER_STATE_COMPLETE:
                db = DataBase()
                config = db.get_config()
                # 判断是否开启了名称过滤模式
                if u'是' in config[2]:
                    self.auth.block_words(self.response)
                    # print self.response.raw
                    # print self.response.new
                    self.client.queue(self.response.new)
                else:
                    self.client.queue(self.response.raw)
        else:
            self.client.queue(data)

    def _access_log(self):
        # host, port = self.server.addr if self.server else (None, None)
        try:
            host = self.request.url.hostname
        except:
            host = None

        try:
            port = self.request.url.port
            if port is None:
                raise
        except:
            port = 80

        if self.request.method == b"CONNECT":
            logger.info(
                "%s:%s - %s %s:%s" % (self.client.addr[0], self.client.addr[1], self.request.method, host, port))
        elif self.request.method:
            logger.info("%s %s:%s - %s %s:%s%s - %s %s - %s bytes" % (
                self.grant,
                self.client.addr[0], self.client.addr[1], self.request.method, host, port, self.request.build_url(),
                self.response.code, self.response.reason, len(self.response.raw)))

    def _get_waitable_lists(self):
        rlist, wlist, xlist = [self.client.conn], [], []
        logger.debug('*** watching client for read ready')

        if self.client.has_buffer():
            logger.debug('pending client buffer found, watching client for write ready')
            wlist.append(self.client.conn)

        if self.server and not self.server.closed:
            logger.debug('connection to server exists, watching server for read ready')
            rlist.append(self.server.conn)

        if self.server and not self.server.closed and self.server.has_buffer():
            logger.debug('connection to server exists and pending server buffer found, watching server for write ready')
            wlist.append(self.server.conn)

        return rlist, wlist, xlist

    # 检测到如果Client.conn是可写状态，则把buffer的数据通过封装函数flush发送（可能发送不完全，没有将整个buffer发送出去)
    # 检测到如果有server，且server未关闭，且Server.conn是可写状态，则flush发送（可能发送不完全)
    def _process_wlist(self, w):
        if self.client.conn in w:
            logger.debug('client is ready for writes, flushing client buffer')
            self.client.flush()

        if self.server and not self.server.closed and self.server.conn in w:
            logger.debug('server is ready for writes, flushing server buffer')
            self.server.flush()

    # 如果client.conn有数据进来，则读取到data，更新最后活跃时间
    # -- 此时如果data是空的，说明连接失败，直接返回True（将导致_process里面循环break，然后退出子进程（因为浏览器连接断开了））
    # -- 此时如果data非空，则进入_process_request(data)来分析请求
    # 如果有server，且未关闭，且server.conn有数据进来，则读取到data，更新最后活跃时间
    # -- 此时如果data是空的，服务器连接关闭（等待下次请求-响应）
    # -- 此时如果data非空，则进入_process_request(data)来分析响应
    def _process_rlist(self, r):
        if self.client.conn in r:
            logger.debug('client is ready for reads, reading')
            data = self.client.recv()
            self.last_activity = self._now()

            if not data:
                logger.debug('client closed connection, breaking')
                return True

            try:
                self._process_request(data)
            except ProxyConnectionFailed as e:
                logger.exception(e)
                self.client.queue(CRLF.join([
                    b'HTTP/1.1 502 Bad Gateway',
                    b'Proxy-agent: proxy.py v' + version,
                    b'Content-Length: 11',
                    b'Connection: close',
                    CRLF
                ]) + b'Bad Gateway')
                self.client.flush()
                return True

        if self.server and not self.server.closed and self.server.conn in r:
            logger.debug('server is ready for reads, reading')
            data = self.server.recv()
            self.last_activity = self._now()

            if not data:
                logger.debug('server closed connection')
                self.server.close()
            else:
                self._process_response(data)

        return False

    # 首先从get_waitable_lists()返回三组socket，目的是分别监控他们是否可读、可写、异常（进入get_waitable看看哪些需要被监控的）
    # 知识点12：select接收三组socket参数，分别监控他们是否可读，可写，发生异常。并且返回这三组socket里面的可读、可写、异常者
    # r、w、x就是经过select的筛选后，得到的本次可读、可写、异常者（异常组应该经常是空的）
    # 然后对可写socket，传入process_wlist函数
    # 以及对可读socket, 传入process_rlist函数（如果还有数据没处理完，则返回False，循环不会break）
    # 如果Cilent的buffer已经为0，且本次响应分析器在结束状态，则循环break(将导致Proxy子进程结束)
    # 如果Cilent的buffer已经为0，且判断为不活跃连接，则循环结束，导致子进程结束
    def _process(self):
        while True:
            rlist, wlist, xlist = self._get_waitable_lists()
            r, w, x = select.select(rlist, wlist, xlist, 1)

            self._process_wlist(w)
            if self._process_rlist(r):
                break

            if self.client.buffer_size() == 0:
                if self.response.state == HTTP_PARSER_STATE_COMPLETE:
                    logger.debug('client buffer is empty and response state is complete, breaking')
                    break

                if self._is_inactive():
                    logger.debug('client buffer is empty and maximum inactivity has reached, breaking')
                    break

    # run函数是Proxy继承自Process必需实现的函数，也是入口
    # 在run函数中只做了日志记录，以及相应的出错善后处理，主要工作放在_process中
    def run(self):
        logger.debug('Proxying connection %r at address %r' % (self.client.conn, self.client.addr))
        try:
            #with PyCallGraph(output=GraphvizOutput(output_file=r'./function_graph/Proxy_%s.png' % time.time())):
            #    self._process()
            self._process()
        except KeyboardInterrupt:
            pass
        except Exception as e:
            logger.exception('Exception while handling connection %r with reason %r' % (self.client.conn, e))
        finally:
            logger.debug(
                "closing client connection with pending client buffer size %d bytes" % self.client.buffer_size())
            self.client.close()
            if self.server:
                logger.debug(
                    "closed client connection with pending server buffer size %d bytes" % self.server.buffer_size())
            self._access_log()
            logger.debug('Closing proxy for connection %r at address %r' % (self.client.conn, self.client.addr))


class TCP(object):
    # HTTP代理服务器的实现代码的父类，实现TCP代理

    # 初始化地址、端口、记录
    def __init__(self, hostname='127.0.0.1', port=8890, backlog=100):
        self.hostname = hostname
        self.port = port
        self.backlog = backlog

    # 将handle函数的实现放在子类HTTP
    def handle(self, client):
        raise NotImplementedError()

    # 创建代理服务器socket，绑定端口
    # 监听端口，接收连接并放到client变量中
    # 调用handle (为client创建单独的代理服务器Proxy类（子进程）)
    def run(self):
        try:
            logger.info('Starting server on port %d' % self.port)
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.socket.bind((self.hostname, self.port))
            self.socket.listen(self.backlog)
            while True:
                conn, addr = self.socket.accept()
                logger.debug('Accepted connection %r at address %r' % (conn, addr))
                client = Client(conn, addr)
                self.handle(client)
        except Exception as e:
            logger.exception('Exception while running the server %r' % e)
        finally:
            logger.info('Closing server socket')
            self.socket.close()


class HTTP(TCP):
    # HTTP代理服务器的实现代码
    # 每调用一次handle，便利用client参数连接创建一个Proxy对象
    # 知识点10：Process类的start函数是一个非阻塞函数，调用之后子进程开始运行，而主进程继续往下走（返回run函数后进入while循环）
    def handle(self, client):
        proc = Proxy(client)
        proc.daemon = True
        proc.start()
        logger.debug('Started process %r to handle connection %r' % (proc, client.conn))


class Authenticator(object):
    def __init__(self):
        self.db = DataBase()
        config = self.db.get_config()
        self.BLACK = True
        self.WHITE = False
        # print config[1]
        self.mode = self.BLACK if u'黑名单' in config[1] else self.WHITE
        # print self.mode

    def block_words(self, response):
        words = self.db.get_words()

        # 如果是gzip格式压缩的HTTP报文
        if response.headers.has_key('content-encoding') and \
                        'gzip' in response.headers['content-encoding']:

            # 取出body部分解压，保存在unzip中
            buf = StringIO(response.body)
            unzip = GzipFile(fileobj=buf).read()
            # print unzip

            # 解压的内容逐个替换敏感词
            for word in words:
                unzip = unzip.replace(word[1].encode(encoding='utf-8'), word[2].encode(encoding='utf-8'))
            # print unzip

            # 将新的内容重新压缩
            buf = StringIO()
            fhandle = gzip.GzipFile(mode='wb', compresslevel=9, fileobj=buf)
            try:
                fhandle.write(unzip)
            finally:
                fhandle.close()

            # 分别计算旧的压缩流长度、新的压缩流长度
            old_len = len(response.body)
            new_len = len(buf.getvalue())
            # print response.raw

            # 替换压缩流，放到response.new当中；替换报文中的内容长度数据
            response.new = response.raw.replace(response.body, buf.getvalue())
            response.new = response.new.replace('%x' % old_len, '%x' % new_len)
            # print response.new

        # 如果是未经压缩的HTTP报文，且头部中有长度数据
        elif response.headers.has_key('content-length'):

            # 获取旧的长度
            old_len = len(response.body)

            # 利用new迭代替换敏感词（replace这个函数将结果作为返回，而不改变原者）
            response.new = response.raw
            for word in words:
                response.new = response.new.replace(word[1].encode(encoding='utf-8'), word[2].encode(encoding='utf-8'))
                response.body = response.body.replace(word[1].encode(encoding='utf-8'),
                                                      word[2].encode(encoding='utf-8'))

            # 计算新的长度，并替换到新的报文中
            new_len = len(response.body)
            response.new = response.new.replace('%d' % old_len, '%d' % new_len)

    def grant(self, request, client):

        # 黑名单模式
        # 对每一条规则，如果能够字段全部符合，则返回False（其中None代表不作限制)
        # 对每一条规则，如果出现任何一个字段的不符合，则if不成立, 回到循环继续检查下一条
        # 匹配完所有规则还没有返回，则没有符合的黑名单项目，返回True
        if self.mode:
            self.rules = self.db.get_black_rules()
            return self._auth(request, client, default=True)

        # 白名单模式
        # 对每一条规则，如果能够字段全部符合，则返回True（其中None代表不作限制)
        # 对每一条规则，如果出现任何一个字段的不符合，则if不成立, 回到循环继续检查下一条
        # 匹配完所有规则还没有返回，则没有符合的白名单项目，返回False
        else:
            self.rules = self.db.get_white_rules()
            return self._auth(request, client, default=False)

    def _auth(self, request, client, default):

        for rule in self.rules:
            if (rule[1] is None or rule[1] in request.method) and \
                    (rule[2] is None or rule[2] in request.url.hostname) and \
                    (rule[3] is None or rule[3] in request.url.port) and \
                    (rule[4] is None or rule[4] in request.url.version):
                parts = request.url.path.split(".")
                filetype = parts[-1] if len(parts) > 1 else 'null'

                if (rule[5] is None or rule[5] in filetype) and \
                        (rule[6] is None or rule[6] in client.addr[0]):

                    if (rule[7] is not None and rule[8] is not None):
                        time_base = time.strftime("%Y-%m-%d-")
                        time1 = float(time.mktime(time.strptime(time_base + rule[7], "%Y-%m-%d-%H:%M")))
                        time2 = float(time.mktime(time.strptime(time_base + rule[8], "%Y-%m-%d-%H:%M")))
                        time_now = float(time.time())

                    if (rule[7] is None or rule[8] is None) or (time_now > time1 and time_now < time2):

                        if (rule[9] is None or rule[10] is None) or \
                                (request.headers.haskey(rule[9]) and rule[10] in request.headers[rule[9]][1]):
                            self.db.write_log(request, client, not default)
                            return not default

        self.db.write_log(request, client, default)
        return default


class DataBase(object):
    def __init__(self):
        self.CONFIG_TABLE = u'配置文件'
        self.BLACK_TABLE = u'黑名单拦截'
        self.WHITE_TABLE = u'白名单模式'
        self.LOG_TABLE = u'访问日志'
        self.WORD_TABLE = u'敏感词屏蔽'
        self.IN_USE = u'启用'
        self.TRUE = u'true'
        self.conn = sqlite3.connect('db.db')

    # 读取配置
    def get_config(self):
        cur = self.conn.cursor()
        cur.execute('select * from %s where %s = \'%s\'' % (self.CONFIG_TABLE, self.IN_USE, self.TRUE))
        config = cur.fetchone()
        return config

    # 读取黑名单规则
    def get_black_rules(self):
        cur = self.conn.cursor()
        cur.execute('select * from %s where %s = \'%s\'' % (self.BLACK_TABLE, self.IN_USE, self.TRUE))
        black_list = cur.fetchall()
        return black_list

    # 读取白名单规则
    def get_white_rules(self):
        cur = self.conn.cursor()
        cur.execute('select * from %s where %s = \'%s\'' % (self.WHITE_TABLE, self.IN_USE, self.TRUE))
        white_list = cur.fetchall()
        return white_list

    # 从敏感词表中读出 启用为true的记录，以LOT的形式返回
    def get_words(self):
        self.WORD = u'敏感词'
        self.SUBSTITUE = u'替代符'
        cur = self.conn.cursor()
        cur.execute('select * from %s where %s = \'%s\'' % (self.WORD_TABLE, self.IN_USE, self.TRUE))
        words = cur.fetchall()
        return words

    def write_log(self, request, client, allow):

        # 首先判断是否启用了日志模式
        cur = self.conn.cursor()
        config = self.get_config()
        if u'是' not in config[3]:
            return

        mins = time.strftime("%H:%M:%S", time.localtime())

        parts = request.url.path.split(".")
        filetype = parts[-1] if len(parts) > 1 else 'null'

        if request.headers.has_key('cookie'):
            cookie = request.headers['cookie'][1]
        else:
            cookie = 'null'

        if request.url.query:
            query = request.url.query
        else:
            query = 'null'
        while True:
            try:
                cur.execute(
                    'insert into %s values (\'%s\', \'%s\',\'%s\',\'%s\',\'%s\',\'%s\',\'%s\',\'%s\',\'%s\',\'%s\',\'%s\',\'%s\')' % \
                    (u'访问日志', mins, allow, request.method, request.version, client.addr[0], client.addr[1], \
                     request.url.hostname, '80', filetype, request.url.path, cookie, query))
                self.conn.commit()
                break
            except Exception, e:
                print e
                raise

    def delete_log(self):
        self.conn.execute('delete from %s where 1 = 1' % u'访问日志')
        self.conn.commit()

    def close_database(self):
        self.conn.close()


# 主函数从命令行读取参数并启动整个HTTP代理服务器：
# 1、主要的两个参数是代理服务器所在的地址和端口，也就是运行在本机的指定端口
# 2、启动初始化HTTP类并进入其run函数(继承自TCP类)
def main():
    parser = argparse.ArgumentParser(
        description='proxy.py v%s' % __version__,
        epilog='Having difficulty using proxy.py? Report at: %s/issues/new' % __homepage__
    )

    db = DataBase()
    Config = db.get_config()
    Port = Config[4]
    db.delete_log()

    parser.add_argument('--hostname', default='127.0.0.1', help='Default: 127.0.0.1')
    parser.add_argument('--port', default='%d' % Port, help='Default: %d' % Port)
    parser.add_argument('--log-level', default='INFO', help='DEBUG, INFO, WARNING, ERROR, CRITICAL')
    args = parser.parse_args()

    logging.basicConfig(level=getattr(logging, args.log_level),
                        format='%(asctime)s - %(levelname)s - pid:%(process)d - %(message)s')

    hostname = args.hostname
    port = int(args.port)

    try:
        proxy = HTTP(hostname, port)

        proxy.run()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
