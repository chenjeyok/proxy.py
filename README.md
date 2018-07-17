proxy.py
========
计算机网络 课程作业
带用户行为控制功能的轻量级HTTP代理服务器

Lightweight HTTP Proxy Server in Python with User's Behavior Control

Features
--------

- 用户上网行为控制、敏感词过滤、上网日志记录
- User's Behavor Control, Specific Words Blocking, Surfing Log
- 兼容gzip压缩格式的网页
- support gzip compressed http page


Usage
-----

```
$ python porxyV0_3.py

所有的配置都在数据库db.db (sqlite3)中, 我现在用SQLite Studion和数据库交互（系统设置、敏感词设置等等）
数据库中的表:

配置文件：   带有运行模式（黑白名单）、是否启用敏感词过滤、是否记录日志、服务器端口等设置
黑名单拦截：  黑名单规则
白名单模式：  白名单规则
访问日志：    访问日志
敏感词屏蔽：  被屏蔽的敏感词，以及其替代符号

Configurations are stored in database db.db(sqlite3)
I'm currently using SQLite3 Studio to interact with the database

tables in db.db
config:         configurations (server port, blocking mode .etc)
black list:     rules to block user's behavior
white list:     rules to allow user's behavior
log:            surfing log
blocked words:  words that are blocked in a html page


Having difficulty using proxy.py? Report at:
https://github.com/chenjeyok/proxy.py/issues/new
or Contact with chenjay@sjtu.edu.cn
```
