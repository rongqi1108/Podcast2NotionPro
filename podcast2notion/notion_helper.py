import logging
import os
import re
import time

from notion_client import Client
from retrying import retry
from datetime import timedelta
from dotenv import load_dotenv
from podcast2notion import utils
load_dotenv()
from podcast2notion.utils import (
    format_date,
    get_date,
    get_first_and_last_day_of_month,
    get_first_and_last_day_of_week,
    get_first_and_last_day_of_year,
    get_icon,
    get_relation,
    get_title,
)

import yaml

# 图标URL
TAG_ICON_URL = "https://www.notion.so/icons/tag_gray.svg"
USER_ICON_URL = "https://www.notion.so/icons/user-circle-filled_gray.svg"
TARGET_ICON_URL = "https://www.notion.so/icons/target_red.svg"
BOOKMARK_ICON_URL = "https://www.notion.so/icons/bookmark_gray.svg"


# Notion 数据库操作助手类，封装常用Notion API操作
class NotionHelper:
    # 数据库名称映射字典
    database_name_dict = {
        "PODCAST_DATABASE_NAME": "Podcast",
        "EPISODE_DATABASE_NAME": "Episode",
        "ALL_DATABASE_NAME": "全部",
        "AUTHOR_DATABASE_NAME": "Author",
        "MINDMAP_DATABASE_NAME": "思维导图",
    }
    database_id_dict = {}  # 存储数据库ID缓存
    heatmap_block_id = None  # 热力图区块ID
    property_dict = {}  # 存储数据库属性结构

    # 初始化Notion客户端和数据库连接
    def __init__(self):
        #  # GitHub Actions环境从环境变量读取
        if os.getenv('GITHUB_ACTIONS') == 'true':
            self.client = Client(auth=os.getenv("NOTION_TOKEN").strip(), log_level=logging.ERROR)
            self.page_id = self.extract_page_id(os.getenv("NOTION_PAGE").strip())
            # 覆盖默认数据库名称（如果环境变量有设置）
            for key in self.database_name_dict.keys():
                if os.getenv(key) != None and os.getenv(key) != "":
                    self.database_name_dict[key] = os.getenv(key)
        else:
            # # 本地开发环境从YAML文件读取
            with open('local_config.yml', 'r', encoding='utf-8') as file:
                config = yaml.safe_load(file)
            self.client = Client(auth=config['NOTION_TOKEN'].strip(), log_level=logging.ERROR)
            self.page_id = self.extract_page_id(config["NOTION_PAGE"].strip())
        self.__cache = {} # 关系ID缓存
        self.search_database(self.page_id)  # 搜索并缓存所有数据库
        # 初始化各数据库ID
        self.episode_database_id = self.database_id_dict.get(
            self.database_name_dict.get("EPISODE_DATABASE_NAME")
        )
        self.podcast_database_id = self.database_id_dict.get(
            self.database_name_dict.get("PODCAST_DATABASE_NAME")
        )
        self.author_database_id = self.database_id_dict.get(
            self.database_name_dict.get("AUTHOR_DATABASE_NAME")
        )
        self.all_database_id = self.database_id_dict.get(
            self.database_name_dict.get("ALL_DATABASE_NAME")
        )
        self.mindmap_database_id = self.database_id_dict.get(
            self.database_name_dict.get("MINDMAP_DATABASE_NAME")
        )
        # 获取Episode数据库属性结构
        r = self.client.databases.retrieve(database_id=self.episode_database_id)
        for key, value in r.get("properties").items():
            self.property_dict[key] = value
        self.day_database_id = self.get_relation_database_id(
            self.property_dict.get("日")
        )
        self.week_database_id = self.get_relation_database_id(
            self.property_dict.get("周")
        )
        self.month_database_id = self.get_relation_database_id(
            self.property_dict.get("月")
        )
        self.year_database_id = self.get_relation_database_id(
            self.property_dict.get("年")
        )
        self.all_database_id = self.get_relation_database_id(
            self.property_dict.get("全部")
        )
        # 初始化数据库结构
        if self.day_database_id:
            self.write_database_id(self.day_database_id)
        if self.podcast_database_id:
            self.update_database(self.podcast_database_id)
        if self.episode_database_id:
            self.update_database(self.episode_database_id)

    # 更新数据库结构（添加缺失属性）   要更新的数据库ID
    @retry(stop_max_attempt_number=3, wait_fixed=5000)
    def update_database(self,database_id):
        response = self.client.databases.retrieve(database_id=database_id)
        id = response.get("id")
        properties = response.get("properties")
        update_properties = {}
        # 检查并添加"通义链接"属性（如果不存在）
        if (
            properties.get("通义链接") is None
            or properties.get("通义链接").get("type") != "url"
        ):
            update_properties["通义链接"] = {"url": {}}
        # 执行更新（如果有需要更新的属性）
        if len(update_properties) > 0:
            self.client.databases.update(database_id=id, properties=update_properties)

    # 从relation属性中提取目标数据库ID  数据库属性对象  return 关联的数据库ID
    def get_relation_database_id(self, property):
        return property.get("relation").get("database_id")

    # 将数据库ID写入GitHub Actions环境文件  要写入的数据库ID
    def write_database_id(self, database_id):
        if os.getenv('GITHUB_ACTIONS') == 'true':
            env_file = os.getenv('GITHUB_ENV')
            # 将值写入环境文件
            with open(env_file, "a") as file:
                file.write(f"DATABASE_ID={database_id}\n")

    # 从Notion URL中提取32位页面ID
    def extract_page_id(self, notion_url):
        # 正则表达式匹配 32 个字符的 Notion page_id
        match = re.search(
            r"([a-f0-9]{32}|[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})",
            notion_url,
        )
        if match:
            return match.group(0)
        else:
            raise Exception(f"获取NotionID失败，请检查输入的Url是否正确")

    # 递归搜索页面下的所有数据库并缓存ID  block_id: 要搜索的页面/区块ID
    def search_database(self, block_id):
        children = self.client.blocks.children.list(block_id=block_id)["results"]
        # 遍历子块
        for child in children:
            # 检查子块的类型  缓存子数据库
            if child["type"] == "child_database":
                self.database_id_dict[child.get("child_database").get("title")] = (
                    child.get("id")
                )
            elif child["type"] == "embed" and child.get("embed").get("url"):
                if child.get("embed").get("url").startswith("https://heatmap.malinkang.com/"):
                    self.heatmap_block_id = child.get("id")
            # 如果子块有子块，递归调用函数
            if "has_children" in child and child["has_children"]:
                self.search_database(child["id"])

    # 更新图片区块的URL
    @retry(stop_max_attempt_number=3, wait_fixed=5000)
    def update_image_block_link(self, block_id, new_image_url):
        # 更新 image block 的链接
        self.client.blocks.update(
            block_id=block_id, image={"external": {"url": new_image_url}}
        )

    def get_week_relation_id(self, date):
        year = date.isocalendar().year
        week = date.isocalendar().week
        week = f"{year}年第{week}周"
        start, end = get_first_and_last_day_of_week(date)
        properties = {"日期": get_date(format_date(start), format_date(end))}
        return self.get_relation_id(
            week, self.week_database_id, self.get_date_icon(date, "week"), properties
        )

    def get_month_relation_id(self, date):
        month = date.strftime("%Y年%m月")
        start, end = get_first_and_last_day_of_month(date)
        year = self.get_year_relation_id(date)
        properties = {
            "日期": get_date(format_date(start), format_date(end)),
            "年": get_relation([year]),
        }
        return self.get_relation_id(
            month, self.month_database_id, self.get_date_icon(date, "month"), properties
        )

    def get_year_relation_id(self, date):
        year = date.strftime("%Y")
        start, end = get_first_and_last_day_of_year(date)
        properties = {"日期": get_date(format_date(start), format_date(end))}
        return self.get_relation_id(
            year, self.year_database_id, self.get_date_icon(date, "year"), properties
        )

    def get_date_icon(self, date, type):
        return f"https://notion-icon.malinkang.com/?type={type}&date={date.strftime('%Y-%m-%d')}"

    def get_day_relation_id(self, date, properties={}):
        new_date = date.replace(hour=0, minute=0, second=0, microsecond=0)
        day = new_date.strftime("%Y年%m月%d日")
        properties["日期"] = get_date(format_date(date))
        return self.get_relation_id(
            day, self.day_database_id, self.get_date_icon(date, "day"), properties
        )

    @retry(stop_max_attempt_number=3, wait_fixed=5000)
    def get_relation_id(self, name, id, icon, properties={}):
        key = f"{id}{name}"
        if key in self.__cache:
            return self.__cache.get(key)
        filter = {"property": "标题", "title": {"equals": name}}
        response = self.client.databases.query(database_id=id, filter=filter)
        if len(response.get("results")) == 0:
            parent = {"database_id": id, "type": "database_id"}
            properties["标题"] = get_title(name)
            page_id = self.client.pages.create(
                parent=parent, properties=properties, icon=get_icon(icon)
            ).get("id")
        else:
            page_id = response.get("results")[0].get("id")
        self.__cache[key] = page_id
        return page_id

    @retry(stop_max_attempt_number=3, wait_fixed=5000)
    def update_book_page(self, page_id, properties):
        return self.client.pages.update(page_id=page_id, properties=properties)

    @retry(stop_max_attempt_number=3, wait_fixed=5000)
    def update_page(self, page_id, properties):
        return self.client.pages.update(page_id=page_id, properties=properties)

    @retry(stop_max_attempt_number=3, wait_fixed=5000)
    def create_page(self, parent, properties, icon):
        return self.client.pages.create(
            parent=parent, properties=properties, icon=icon, cover=icon
        )

    @retry(stop_max_attempt_number=3, wait_fixed=5000)
    def query(self, **kwargs):
        kwargs = {k: v for k, v in kwargs.items() if v}
        return self.client.databases.query(**kwargs)

    @retry(stop_max_attempt_number=3, wait_fixed=5000)
    def get_block_children(self, id):
        response = self.client.blocks.children.list(id)
        return response.get("results")

    @retry(stop_max_attempt_number=3, wait_fixed=5000)
    def append_blocks(self, block_id, children):
        return self.client.blocks.children.append(block_id=block_id, children=children)

    @retry(stop_max_attempt_number=3, wait_fixed=5000)
    def append_blocks_after(self, block_id, children, after):
        return self.client.blocks.children.append(
            block_id=block_id, children=children, after=after
        )

    @retry(stop_max_attempt_number=3, wait_fixed=5000)
    def delete_block(self, block_id):
        return self.client.blocks.delete(block_id=block_id)

    @retry(stop_max_attempt_number=3, wait_fixed=5000)
    def query_all_by_filter(self, database_id, filter,sorts):
        results = []
        has_more = True
        start_cursor = None
        while has_more:
            response = self.client.databases.query(
                database_id=database_id,
                filter=filter,
                sorts=sorts,
                start_cursor=start_cursor,
                page_size=100,
            )
            start_cursor = response.get("next_cursor")
            has_more = response.get("has_more")
            results.extend(response.get("results"))
        return results
    
    @retry(stop_max_attempt_number=3, wait_fixed=5000)
    def get_all_podcast(self):
        results = self.query_all(self.podcast_database_id)
        podcast_dict = {}
        for result in results:
            pid = utils.get_property_value(result.get("properties").get("Pid"))
            podcast_dict[pid] = {
                "page_id": result.get("id"),
                "最后更新时间": utils.get_property_value(
                    result.get("properties").get("最后更新时间")
                ),
                "收听时长": utils.get_property_value(
                    result.get("properties").get("收听时长")
                ),
                "通义链接": utils.get_property_value(result.get("properties").get("通义链接"))
                
            }
        return podcast_dict
    
    @retry(stop_max_attempt_number=3, wait_fixed=5000)
    def get_all_episode(self):
        results = self.query_all(self.episode_database_id)
        episode_dict = {}
        for result in results:
            eid = utils.get_property_value(result.get("properties").get("Eid"))
            episode_dict[eid] = {
                "page_id": result.get("id"),
                "状态": utils.get_property_value(
                    result.get("properties").get("状态")
                ),
                "喜欢": utils.get_property_value(
                    result.get("properties").get("喜欢")
                ),          
                "收听进度": utils.get_property_value(
                    result.get("properties").get("收听进度")
                ),          
                "语音转文字状态": utils.get_property_value(
                    result.get("properties").get("语音转文字状态")
                ),
                "通义链接": utils.get_property_value(
                    result.get("properties").get("通义链接")
                ),
                "日期": utils.get_property_value(
                    result.get("properties").get("日期")
                )
            }
        return episode_dict

    @retry(stop_max_attempt_number=3, wait_fixed=5000)
    def query_all(self, database_id):
        """获取database中所有的数据"""
        results = []
        has_more = True
        start_cursor = None
        while has_more:
            response = self.client.databases.query(
                database_id=database_id,
                start_cursor=start_cursor,
                page_size=100,
            )
            start_cursor = response.get("next_cursor")
            has_more = response.get("has_more")
            results.extend(response.get("results"))
        return results

    def get_all_relation(self, properties):
        properties["全部"] = get_relation(
            [
                self.get_relation_id("全部", self.all_database_id, TARGET_ICON_URL),
            ]
        )

    def get_date_relation(self, properties, date):
        properties["年"] = get_relation(
            [
                self.get_year_relation_id(date),
            ]
        )
        properties["月"] = get_relation(
            [
                self.get_month_relation_id(date),
            ]
        )
        properties["周"] = get_relation(
            [
                self.get_week_relation_id(date),
            ]
        )
        properties["日"] = get_relation(
            [
                self.get_day_relation_id(date),
            ]
        )

    # 更新热力图区块URL
    @retry(stop_max_attempt_number=3, wait_fixed=5000)
    def update_heatmap(self, block_id, url):
        # 更新 image block 的链接
        return self.client.blocks.update(block_id=block_id, embed={"url": url})
