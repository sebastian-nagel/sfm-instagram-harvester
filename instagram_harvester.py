#!/usr/bin/env python

from instascrape import Profile, scrape_posts

import logging
import re
import json
from bs4 import BeautifulSoup
from warcio.warcwriter import WARCWriter
import requests
import os
import datetime
from io import BytesIO
import warcprox
import random
import time
from selenium import webdriver
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.desired_capabilities import DesiredCapabilities
from selenium.webdriver.support import expected_conditions as ec
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait


from sfmutils.harvester import BaseHarvester, Msg, CODE_TOKEN_NOT_FOUND, CODE_UID_NOT_FOUND, CODE_UNKNOWN_ERROR
from sfmutils.warcprox import warced
from sfmutils.utils import safe_string

log = logging.getLogger(__name__)

QUEUE = "instagram_rest_harvester"
TIMELINE_ROUTING_KEY = "harvest.start.instagram.instagram_user_timeline"
PROFILE_ROUTING_KEY = "harvest.start.instagram.instagram_user_profile"

BASE_INSTAGRAM_URL = "https://www.instagram.com/"

class InstagramHarvester(BaseHarvester):
    """SFM class for scraping instagram posts or profile data"""

    def __init__(self, working_path, stream_restart_interval_secs=30 * 60, mq_config=None,debug=False,
                 connection_errors=5, http_errors=5, debug_warcprox=False, tries=3):
        BaseHarvester.__init__(self, working_path, mq_config=mq_config, use_warcprox = True,
                               stream_restart_interval_secs=stream_restart_interval_secs,
                               debug=debug, debug_warcprox=debug_warcprox, tries=tries)

        self.connection_errors = connection_errors
        self.http_errors = http_errors
        self.harvest_media_types = { 'photo': True }
        self.local = False # todo


    def initiate_selenium_webdriver_local(self):
        """
        Instantiates local selenium webdriver without docker.
        """
        driver = webdriver.Firefox(executable_path=r'/mnt/c/Users/Frederik Gremler/Documents/EO2/harvester/selenium/geckodriver.exe')
        return driver

    def initiate_selenium_webdriver(self):
        """
        Instantiates selenium webdriver with docker connection for sfm
        Closing should take place outside of this function! (reused from sfm-facebookharvester)
        """
        user_agent = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/60.0.3112.50 Safari/537.36'

        chrome_options = webdriver.ChromeOptions()
        chrome_options.add_argument('headless')
        chrome_options.add_argument('start-maximised')
        chrome_options.add_argument('--no-sandbox')
        chrome_options.add_argument('--window-size=1200x800')
        chrome_options.add_argument('--disable-dev-shm-usage')
        chrome_options.add_argument(f"user-agent={user_agent}")

        # this will connect to the selenium container starting scraping
        # Note: host name of the running container is "selenium"
        driver = webdriver.Remote("http://selenium:4444/wd/hub", {'browserName': 'chrome'}, options=chrome_options)
        return driver

    def insta_login(self, driver):
        """Logs into instagram and returns the respective webdriver session via selenium.
        Closing has to happen elsewhere!"""

        user_email = self.message['credentials']['user_email_ins']
        user_password = self.message['credentials']['user_password_ins']

        driver.get(BASE_INSTAGRAM_URL)
        driver.maximize_window()

        # accept cookies
        cookies = driver.find_element_by_css_selector("button.aOOlW:nth-child(2)")

        cookies.click()

        time.sleep(random.uniform(3,9))

        username_ins = driver.find_element_by_name('username')
        password_ins = driver.find_element_by_name('password')
        submit = driver.find_element_by_css_selector("button[type='submit']")

        time.sleep(random.uniform(3,9))

        # send keys and make sure not prepolutaed
        # 2fa has to be deactivated
        username_ins.clear()
        password_ins.clear()
        username_ins.send_keys(user_email)
        password_ins.send_keys(user_password)

        time.sleep(random.uniform(3,9))
        # Step 4) Click Login
        submit.click()

        time.sleep(random.uniform(3,9))

        return(driver)

    def harvest_seeds(self):

        harvest_type = self.message.get("type")

        if harvest_type == "instagram_user_timeline":
            log.debug("Starting Instagram Timeline Harvest")
            self.instagram_users_timeline()
        elif harvest_type == "instagram_user_profile":
            log.debug("Starting Instagram Profile Harvest")
            self.instagram_users_profile()
        else:
            raise KeyError


    def instagram_users_timeline(self):
        """Several insta users"""
        log.debug("Harvesting users with seeds %s", self.message.get("seeds"))

        for seed in self.message.get("seeds", []):
            print(seed)
            self.instagram_user_timeline(username = seed.get('token'))

    def instagram_user_timeline(self, username):
        """Scrapes single user's post """
        assert username
        incremental = self.message.get("options", {}).get("incremental", False)
        harvest_media = self.message.get("options", {}).get("harvest_media", False)

        # if no cookies exist, get them
        if not os.path.exists("cookies.json"):
            driver = self.initiate_selenium_webdriver()
            self.insta_login(driver = driver)

            cookies = driver.get_cookies()

            with open("cookies.json", "w") as f:
                json.dump(cookies, f)


        # prepare selenium session and login
        if self.local:
            driver = self.initiate_selenium_webdriver_local()
        else:
            driver = self.initiate_selenium_webdriver()

        # after this cookies should be present but check anyways
        # check whether cookies are present, otherwise try to
        # log in
        if os.path.isfile("cookies.json"):
            # first navigate to fb, otherwise
            # selenium does not accept the cookies
            # navigate to page
            driver.get("https://www.instagram.com/")

            #  load cookies
            with open("cookies.json") as f:
                cookies = json.load(f)

            # add to driver
            for cookie in cookies: driver.add_cookie(cookie)

        # if no cookies, try to login
        else:
            self.fb_login(driver = driver)
            time.sleep(random.uniform(3,9))

        # retrieve the session id (apparently needed for profile.scrape...)
        SESSIONID = driver.session_id

        headers = {'User-Agent': "Mozilla/5.0 (Linux; Android 6.0; Nexus 5 Build/MRA58N) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/87.0.4280.88 Mobile Safari/537.36 Edg/87.0.664.57",
                   "cookie": f"sessionid={SESSIONID}"}

                # instantiate insta-scrape Profile class
        # check that https://www.instagram.com does NOT precede it
        # username str sub for insta
        if username.startswith("https://www.instagram.com/"):
            username = username.replace("https://www.instagram.com/", "")
            username = username.replace("/", "")

        insta_profile = Profile(username)
        # insta_profile.scrape(headers = headers)
        insta_profile.scrape(webdriver=driver)

        # now scrape posts url
        # also setting a pause between requests to avoid blocking
        # also increasing failed scrolls up to 400
        log.info("Collecting Posts")
        time.sleep(10)
        # todo reset amount
        if incremental:
            since_id = self.state_store.get_state(__name__, u"timeline.{}.since_id".format(username))

        posts = []
        # this gets the post ids so that they later can be scraped AND checks whether the post was scraped beforehand
        # amount = 5 or something else can be used to limit post number
        for post in insta_profile.get_posts(webdriver = driver, scrape_pause = 5, max_failed_scroll = 400):

            if incremental and post["source"] == since_id and post["source"]:
                log.info("Stopping, found last post that was previously harvested with id: %s", post["source"])
                break

            posts.append(post)

        # ready to scrape individual posts
        # will scrape individual posts with selenium - pause is set even higher here
        # with 1 post = 10 seconds - very active insta pages will thus
        # take a very long time
        log.info("Scraping Posts")
        time.sleep(10)
        # todo - should work without this!
        # scraped_posts, unscraped_posts = scrape_posts(posts, webdriver = driver, pause = 10, silent = False)
        # all_posts = [post.to_dict() for post in scraped_posts]
        # print(all_posts)

        all_posts = scrape_posts(posts, webdriver = driver, pause = 5, silent = False)
        # print(all_posts)
        # scrape_posts()[1] would be failed scraped posts
        all_posts = all_posts[0]
        # print(all_posts)

        # make sure the driver always quits otherways it will keep open
        # and cause problems the next time around (https://www.youtube.com/watch?v=O_I6TJAKvH8)
        driver.quit()

        for post in all_posts:
            self.result.harvest_counter["posts"] += 1
            self.result.increment_stats("posts")

            # media is captured by warcprox
            if harvest_media and post['display_url']:
                self._harvest_media_url(post['display_url'])
                time.sleep(random.uniform(3,9))

        all_posts = [post.to_dict() for post in all_posts]

        # finally set state for incremental harvests
        if incremental:
            key = "timeline.{}.since_id".format(username)
            max_post_id = all_posts[0]["shortcode"] # first post should be most recent one
            self.state_store.set_state(__name__, key, max_post_id)
            log.info("Wrote first, most recent scraped post to state_store: %s (state: %s)", max_post_id, key)
        # rationale for state:
        # todo


        # write content to separate warc (same strategy as with facebook scraper)
        random_token = ''.join(random.sample('abcdefghijklmnopqrstuvwxyz0123456789', 8))
        serial_no = '00000'
        file_name = safe_string(self.message["id"]) + "-" + warcprox.timestamp17() + "-" + serial_no + "-" + random_token

        with open(os.path.join(self.warc_temp_dir, file_name + ".warc.gz"), "wb") as result_warc_file:
            log.info("Writing json-timeline result to path %s", self.warc_temp_dir)
            writer = WARCWriter(result_warc_file, gzip = True)

            def json_date_converter(o):
                """ Converts datetime.datetime items in facebook_scraper result
                to formate suitable for json.dumps"""
                if isinstance(o, datetime.datetime):
                    return o.__str__()

            # todo json conversion implemented in package so...
            json_payload = json.dumps(all_posts, default = json_date_converter,
                                      ensure_ascii = False).encode("utf-8")


            record = writer.create_warc_record(username, 'metadata',
                                                payload = BytesIO(json_payload),
                                                warc_content_type = "application/json")
            writer.write_record(record)
            log.info("Writing scraped results to %s", self.warc_temp_dir)

    def _harvest_media_url(self, url):
        log.debug("Harvesting media URL %s", url)
        try:
            r = requests.get(url)
            log.info("Harvested media URL %s (status: %i, content-type: %s)",
                     url, r.status_code, r.headers['content-type'])
        except Exception:
            log.exception("Failed to harvest media URL %s with exception:", url)



if __name__ == "__main__":
    InstagramHarvester.main(InstagramHarvester, QUEUE, [TIMELINE_ROUTING_KEY, PROFILE_ROUTING_KEY])
