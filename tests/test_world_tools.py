import base64
import unittest
from unittest import mock

from tools.world import clock, location, rss, web_reader, web_search, weather


class ClockTest(unittest.TestCase):
    def test_now_text_has_date(self):
        text = clock.now_text()
        self.assertIn("星期", text)
        self.assertIn(":", text)

    def test_weekday_offset(self):
        self.assertIn("星期", clock.weekday_text(offset_days=1))

    def test_countdown(self):
        text = clock.countdown_text("2099-01-01")
        self.assertIn("还有", text)

    def test_countdown_past(self):
        self.assertIn("已经过去", clock.countdown_text("2000-01-01"))

    def test_handle_text_time_question(self):
        self.assertIn("现在", clock.handle_text("现在几点了"))

    def test_handle_text_weekday(self):
        self.assertIn("星期", clock.handle_text("明天星期几"))


class WebReaderTest(unittest.TestCase):
    HTML = """
    <html><head><title>测试标题</title>
    <script>var x = '脚本不该出现';</script>
    <style>.a{color:red}</style></head>
    <body><nav>导航不该出现</nav>
    <article><p>这是正文的第一段，写的是很实在的内容。</p>
    <p>这是正文的第二段，同样有足够多的字来被识别为正文。</p></article>
    <footer>页脚不该出现</footer></body></html>
    """

    def test_extract_title_and_body(self):
        title, body = web_reader.extract_html(self.HTML)
        self.assertEqual(title, "测试标题")
        self.assertIn("正文的第一段", body)
        self.assertNotIn("脚本不该出现", body)
        self.assertNotIn("导航不该出现", body)
        self.assertNotIn("页脚不该出现", body)

    def test_truncate(self):
        _, body = web_reader.extract_html("<p>" + "字" * 5000 + "</p>", limit=100)
        self.assertLessEqual(len(body), 100)

    def test_rejects_non_url(self):
        self.assertFalse(web_reader.read_url("不是链接").ok)

    def test_read_url_with_mock_transport(self):
        with mock.patch("tools.world.http.get", return_value=self.HTML.encode("utf-8")):
            result = web_reader.read_url("https://example.com/a")
        self.assertTrue(result.ok)
        self.assertIn("正文的第一段", result.text)
        self.assertEqual(result.http_requests, 1)


class WebSearchTest(unittest.TestCase):
    DDG = """
    <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa">标题一</a>
    <a class="result__snippet" href="#">摘要一</a>
    <a class="result__a" href="https://example.com/b">标题二</a>
    <a class="result__snippet" href="#">摘要二</a>
    """
    BING = (
        '<ol id="b_results"><li class="b_algo"><link rel="stylesheet" href="/rp/x.css"/>'
        '<h2 class=""><a target="_blank" href="https://example.com/c" h="ID=SERP,1">标题三</a></h2>'
        '<div class="b_caption"><p class="b_lineclamp2">摘要三</p></div></li>'
        '<li class="b_algo"><h2><a href="https://example.com/d">标题四</a></h2></li></ol>'
    )
    BLOCK_PAGE = '<div class="anomaly-modal">Unfortunately, bots use DuckDuckGo too.</div>'

    def test_parse_duckduckgo(self):
        results = web_search.parse_duckduckgo(self.DDG)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["title"], "标题一")
        self.assertEqual(results[0]["url"], "https://example.com/a")
        self.assertEqual(results[0]["snippet"], "摘要一")

    def test_parse_bing(self):
        results = web_search.parse_bing(self.BING)
        self.assertEqual(results[0]["url"], "https://example.com/c")

    def test_search_uses_bing_first(self):
        with mock.patch("tools.world.http.get", return_value=self.BING.encode("utf-8")) as transport:
            result = web_search.search("标题")
        self.assertTrue(result.ok)
        self.assertEqual(result.data["engine"], "bing")
        self.assertEqual(transport.call_count, 1)
        self.assertEqual(result.http_requests, 1)

    def test_search_falls_back_to_duckduckgo(self):
        with mock.patch(
            "tools.world.http.get",
            side_effect=[self.BLOCK_PAGE.encode("utf-8"), self.DDG.encode("utf-8")],
        ):
            result = web_search.search("标题")
        self.assertTrue(result.ok)
        self.assertEqual(result.data["engine"], "duckduckgo")
        self.assertEqual(result.http_requests, 2)

    def test_blocked_engines_are_reported_without_hammering(self):
        with mock.patch("tools.world.http.get", return_value=self.BLOCK_PAGE.encode("utf-8")) as transport:
            result = web_search.search("标题")
        self.assertFalse(result.ok)
        self.assertIn("验证页", result.error)
        self.assertLessEqual(transport.call_count, 2)   # 不硬刷
        self.assertIn("bing", result.data["blocked"])

    def test_bing_redirect_is_unwrapped(self):
        target = "https://www.zhihu.com/"
        payload = "a1" + base64.urlsafe_b64encode(target.encode()).decode().rstrip("=")
        html = (
            '<li class="b_algo"><h2><a href="https://www.bing.com/ck/a?!&&p=x&u='
            + payload
            + '&ntb=1">知乎</a></h2></li>'
        )
        results = web_search.parse_bing(html)
        self.assertEqual(results[0]["url"], target)

    def test_block_page_detection(self):
        self.assertTrue(web_search.is_block_page(self.BLOCK_PAGE))
        self.assertFalse(web_search.is_block_page(self.BING))

    def test_spam_results_are_filtered(self):
        spam = {"title": "Sex doll casino", "url": "https://mydesi2.net/x", "snippet": "creampie"}
        good = {"title": "Python 3.13 新特性", "url": "https://docs.python.org/3/whatsnew/3.13.html", "snippet": "官方说明"}
        self.assertTrue(web_search.looks_like_spam(spam))
        kept = web_search.clean_results([spam, good], limit=5)
        self.assertEqual([item["url"] for item in kept], [good["url"]])

    def test_trusted_domains_rank_first(self):
        random_site = {"title": "某博客", "url": "https://random-blog.example/post", "snippet": ""}
        official = {"title": "官方文档", "url": "https://docs.python.org/3/whatsnew/3.13.html", "snippet": ""}
        ranked = web_search.rank_results([random_site, official])
        self.assertEqual(ranked[0]["url"], official["url"])

    def test_all_spam_returns_honest_failure(self):
        page = (
            '<li class="b_algo"><h2><a href="https://spam.example/a">sex casino</a></h2>'
            '<p>creampie gambling</p></li>'
        )
        with mock.patch("tools.world.http.get", return_value=page.encode("utf-8")):
            result = web_search.search("随便搜点东西")
        self.assertFalse(result.ok)
        self.assertIn("垃圾", result.error)

    def test_search_marks_filtered_count(self):
        page = (
            '<li class="b_algo"><h2><a href="https://docs.python.org/3/x">官方</a></h2><p>很好</p></li>'
            '<li class="b_algo"><h2><a href="https://spam.example/b">casino</a></h2><p>bet</p></li>'
        )
        with mock.patch("tools.world.http.get", return_value=page.encode("utf-8")):
            result = web_search.search("官方")
        self.assertTrue(result.ok)
        self.assertEqual(result.data["filtered"], 1)

    def test_irrelevant_results_are_rejected(self):
        page = (
            '<li class="b_algo"><h2><a href="https://clothing.example/a">男士服装店</a></h2>'
            '<p>Indianapolis 的一家服装店</p></li>'
        )
        with mock.patch("tools.world.http.get", return_value=page.encode("utf-8")):
            result = web_search.search("Python 3.13 新特性")
        self.assertFalse(result.ok)
        self.assertIn("对不上", result.error)

    def test_relevance_helper(self):
        results = [{"title": "Python 3.13 新特性", "url": "https://docs.python.org", "snippet": ""}]
        self.assertTrue(web_search.relevant(results, "Python 3.13"))
        self.assertFalse(web_search.relevant(results, "男士服装"))

    def test_api_search_preferred_when_configured(self):
        payload = {"results": [{"title": "官方文档", "url": "https://docs.python.org/3", "content": "新特性"}]}
        with mock.patch("tools.world.http.post_json", return_value=payload) as api:
            result = web_search.search("python", api_key="k", api_url="https://api.example/search")
        self.assertTrue(result.ok)
        self.assertEqual(result.data["engine"], "api")
        self.assertEqual(result.http_requests, 1)
        api.assert_called_once()

    def test_api_failure_reports_honestly(self):
        with mock.patch("tools.world.http.post_json", side_effect=RuntimeError("401")):
            result = web_search.search("python", api_key="k", api_url="https://api.example/search")
        self.assertFalse(result.ok)
        self.assertIn("搜索接口", result.error)

    def test_search_empty_query(self):
        self.assertFalse(web_search.search("  ").ok)


class WeatherTest(unittest.TestCase):
    def test_weather_uses_cache_friendly_result(self):
        geocode = {"results": [{"name": "香港", "latitude": 22.3, "longitude": 114.2, "country": "中国"}]}
        forecast = {
            "current": {
                "temperature_2m": 28.5, "relative_humidity_2m": 80, "apparent_temperature": 31,
                "weather_code": 61, "wind_speed_10m": 12,
            },
            "daily": {"temperature_2m_max": [31], "temperature_2m_min": [26],
                      "precipitation_probability_max": [70]},
        }
        with mock.patch("tools.world.http.get_json", side_effect=[geocode, forecast]):
            result = weather.fetch("香港")
        self.assertTrue(result.ok)
        self.assertIn("28.5", result.text)
        self.assertIn("小雨", result.text)
        self.assertEqual(result.http_requests, 2)
        self.assertEqual(result.data["source"], "open-meteo")

    def test_weather_unknown_city(self):
        with mock.patch("tools.world.http.get_json", return_value={"results": []}):
            result = weather.fetch("不存在的地方")
        self.assertFalse(result.ok)

    def test_weather_without_city(self):
        self.assertFalse(weather.fetch("").ok)


class LocationTest(unittest.TestCase):
    def test_lookup(self):
        payload = {"results": [{"name": "株洲", "admin1": "湖南省", "country": "中国",
                                "latitude": 27.8, "longitude": 113.1}]}
        with mock.patch("tools.world.http.get_json", return_value=payload):
            result = location.lookup("湖南工业大学")
        self.assertTrue(result.ok)
        self.assertIn("株洲", result.text)

    def test_lookup_empty(self):
        self.assertFalse(location.lookup("").ok)


class RssTest(unittest.TestCase):
    FEED = """<?xml version="1.0"?><rss><channel>
    <item><title>第一条要闻</title><link>https://example.com/1</link></item>
    <item><title>第二条要闻</title><link>https://example.com/2</link></item>
    </channel></rss>"""

    def test_fetch_titles(self):
        with mock.patch("tools.world.http.get", return_value=self.FEED.encode("utf-8")):
            result = rss.fetch("https://example.com/feed.xml")
        self.assertTrue(result.ok)
        self.assertIn("第一条要闻", result.text)
        self.assertEqual(result.http_requests, 1)

    def test_bad_feed(self):
        with mock.patch("tools.world.http.get", return_value=b"not xml"):
            self.assertFalse(rss.fetch("https://example.com/x").ok)

    def test_no_url(self):
        self.assertFalse(rss.fetch("").ok)


if __name__ == "__main__":
    unittest.main()
