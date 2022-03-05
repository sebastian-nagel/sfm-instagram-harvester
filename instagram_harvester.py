#!/usr/bin/env python

import instagram_scraper

import logging
import json
from warcio.warcwriter import WARCWriter
import requests
import os
import datetime
from io import BytesIO
import warcprox
import random
import time
import pickle

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
            self.instagram_user_timeline(username=seed.get('token'))

    def instagram_user_timeline(self, username):
        """Scrapes single user's post """
        assert username
        incremental = self.message.get("options", {}).get("incremental", False)
        harvest_media = self.message.get("options", {}).get("harvest_media", False)

        # check that https://www.instagram.com does NOT precede it
        # username str sub for insta
        if username.startswith("https://www.instagram.com/"):
            username = username.replace("https://www.instagram.com/", "")
            username = username.replace("/", "")

        # now scrape posts url
        # also setting a pause between requests to avoid blocking
        # also increasing failed scrolls up to 400
        log.info("Collecting Posts")
        time.sleep(10)
        # todo reset amount
        if incremental:
            since_id = self.state_store.get_state(__name__, u"timeline.{}.since_id".format(username))

        # create cookiejar if not exits
        if not os.path.exists("/tmp/cookie_jar_instascrape"):
            user_email = self.message['credentials']['user_email_ins']
            user_password = self.message['credentials']['user_password_ins']
            args = {"login_user": user_email, "login_pass": user_password, "logger": False}
            insta_scraper = instagram_scraper.InstagramScraper(**args)
            insta_scraper.authenticate_with_login()

            # save cookies
            if insta_scraper.session.cookies:
                with open("/tmp/cookie_jar_instascrape", "wb") as f:
                    pickle.dump(insta_scraper.session.cookies, f)
            else:
                log.error("Could not find cookies after logging in. Stopping.")
                return

        # start scraping by instantiating sesseion class
        insta_scraper = instagram_scraper.InstagramScraper(cookiejar="/tmp/cookie_jar_instascrape", log_destination = "/tmp/")
        # get info
        shared_info = insta_scraper.get_shared_data_userinfo(username)

        posts = []
        counter = 0
        # this gets the post ids so that they later can be scraped AND checks whether the post was scraped beforehand
        # amount = 5 or something else can be used to limit post number
        for post in insta_scraper.query_media_gen(shared_info):

            self.result.harvest_counter["posts"] += 1
            self.result.increment_stats("posts")
            counter += 1
            # for very long harvests, try to avoid blocking by sleeping after a
            # certain amount of posts
            if self.result.harvest_counter["posts"] in [150, 400, 800]:
                log.info("Waiting a few minutes to avoid block bc of too many requests")
                time.sleep(random.uniform(50, 450))
            # if posts > 1000 for that harvest we just break to avoid blocking!
            if counter > 1000:
                log.info("Reached 1000 Insta posts. Stopping.")
                break

            if incremental and post.get("id") == since_id:
                log.info("Stopping, found last post that was previously harvested with id: %s", post["id"])
                break
            # just append the whole dict
            posts.append(post)
            # add random sleep to avoid blocks
            time.sleep(random.uniform(0.3, 2.5))


            # media is captured by warcprox
            if harvest_media and post['thumbnail_src']:
                self._harvest_media_url(post['thumbnail_src'])
                time.sleep(random.uniform(2,5))

        # finally set state for incremental harvests
        if incremental and len(posts)>0:
            key = "timeline.{}.since_id".format(username)
            max_post_id = posts[0]["id"] # first post should be most recent one
            self.state_store.set_state(__name__, key, max_post_id)
            log.info("Wrote first, most recent scraped post to state_store: %s (state: %s)", max_post_id, key)

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
            json_payload = json.dumps(posts, default = json_date_converter,
                                      ensure_ascii = False).encode("utf-8")


            record = writer.create_warc_record(username, 'metadata',
                                                payload = BytesIO(json_payload),
                                                warc_content_type = "application/json")
            writer.write_record(record)
            log.info("Writing scraped results to %s", self.warc_temp_dir)
            # sleep between harvests
            log.info("Sleeping a few minutes to avoid too many subsequent harvests.")
            time.sleep(random.randint(850, 1500))

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
