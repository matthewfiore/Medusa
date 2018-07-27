# coding=utf-8
# Author: Nic Wolfe <nic@wolfeden.ca>

#
# This file is part of Medusa.
#
# Medusa is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Medusa is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Medusa. If not, see <http://www.gnu.org/licenses/>.

# pylint: disable=line-too-long

from __future__ import unicode_literals

import logging
import re

from medusa.helper.encoding import ss
from medusa.helper.exceptions import ex
from medusa.logger.adapters.style import BraceAdapter
from medusa.name_parser.parser import InvalidNameException, InvalidShowException, NameParser
from medusa.search.result import NZBDataSearchResult
from medusa.session.core import MedusaSession

try:
    import xml.etree.cElementTree as ETree
except ImportError:
    import xml.etree.ElementTree as ETree


log = BraceAdapter(logging.getLogger(__name__))
log.logger.addHandler(logging.NullHandler())

session = MedusaSession()


def get_season_nzbs(name, url_data, season):
    """
    Split a season NZB into episodes

    :param name: NZB name
    :param url_data: URL to get data from
    :param season: Season to check
    :return: dict of (episode files, xml matches)
    """

    # TODO: clean up these regex'es, comment them, and make them all raw strings
    regex_string = {
        # Match the xmlns in an nzb
        # Example:  nzbElement.getchildren()[1].tag == '{http://www.newzbin.com/DTD/2003/nzb}file'
        #           regex match returns  'http://www.newzbin.com/DTD/2003/nzb'
        'nzb_xmlns': r"{(http://[\w_\./]+nzb)}file",
        'scene_name': '([\w\._\ ]+)[\. ]S%02d[\. ]([\w\._\-\ ]+)[\- ]([\w_\-\ ]+?)',
        'episode': '\.S%02d(?:[E0-9]+)\.[\w\._]+\-\w+',
    }

    try:
        show_xml = ETree.ElementTree(ETree.XML(url_data))
    except SyntaxError:
        log.error(u'Unable to parse the XML of {0}, not splitting it', name)
        return {}, ''

    nzb_element = show_xml.getroot()

    scene_name_match = re.search(regex_string['scene_name'] % season, name, re.I)
    if scene_name_match:
        show_name = scene_name_match.groups()[0]
    else:  # Make sure we aren't missing valid results after changing name_parser and the quality detection
        # Most of these will likely be invalid shows
        log.debug(u'Unable to parse {0} into a scene name.', name)
        return {}, ''

    regex = '(' + re.escape(show_name) + regex_string['episode'] % season + ')'
    regex = regex.replace(' ', '.')

    ep_files = {}
    xmlns = None

    for cur_file in nzb_element.getchildren():
        xmlns_match = re.match(regex_string['nzb_xmlns'], cur_file.tag)
        if not xmlns_match:
            continue
        else:
            xmlns = xmlns_match.group(1)
        match = re.search(regex, cur_file.get("subject"), re.I)
        if not match:
            # regex couldn't match cur_file.get("subject")
            continue
        cur_ep = match.group(1)
        if cur_ep not in ep_files:
            ep_files[cur_ep] = [cur_file]
        else:
            ep_files[cur_ep].append(cur_file)
    # TODO: Decide what to do if we found multiple valid xmlns strings, should we only return the last???
    return ep_files, xmlns


def create_nzb_string(file_elements, xmlns):
    """
    Extract extra info from file_elements.

    :param file_elements: to be processed
    :param xmlns: the xml namespace to be used
    :return: string containing all extra info extracted from the file_elements
    """
    root_element = ETree.Element("nzb")
    if xmlns:
        root_element.set("xmlns", xmlns)

    for cur_file in file_elements:
        root_element.append(strip_xmlns(cur_file, xmlns))

    return ETree.tostring(ss(root_element))


def save_nzb(nzb_name, nzb_string):
    """
    Save NZB to disk

    :param nzb_name: Filename/path to write to
    :param nzb_string: Content to write in file
    """
    try:
        with open(nzb_name + ".nzb", 'w') as nzb_fh:
            nzb_fh.write(nzb_string)

    except EnvironmentError as error:
        log.error(u'Unable to save NZB: {0}', ex(error))


def strip_xmlns(element, xmlns):
    """
    Remove the xml namespace from the element's children.

    :param element: to be processed
    :param xmlns: xml namespace to be removed
    :return: processed element
    """
    element.tag = element.tag.replace("{" + xmlns + "}", "")
    for cur_child in element.getchildren():
        strip_xmlns(cur_child, xmlns)

    return element


def split_result(obj):
    """
    Split obj into separate episodes.

    :param obj: to search for results
    :return: a list of episode objects or an empty list
    """
    # TODO: Check if this needs exception handling.
    url_data = session.get(obj.url).content
    if url_data is None:
        log.error(u'Unable to load url {0}, can\'t download season NZB', obj.url)
        return []

    # parse the season ep name
    try:
        parsed_obj = NameParser(series=obj.series).parse(obj.name)
    except (InvalidNameException, InvalidShowException) as error:
        log.debug(u'{}', error)
        return []

    # bust it up
    season = 1 if parsed_obj.season_number is None else parsed_obj.season_number

    separate_nzbs, xmlns = get_season_nzbs(obj.name, url_data, season)

    result_list = []

    # TODO: Re-evaluate this whole section
    #   If we have valid results and hit an exception, we ignore the results found so far.
    #   Maybe we should return the results found or possibly continue with the next iteration of the loop
    #   Also maybe turn this into a function and generate the results_list with a list comprehension instead
    for new_nzb in separate_nzbs:
        log.debug(u'Split out {new_nzb} from {name}', {'new_nzb': new_nzb, 'name': obj.name})

        # parse the name
        try:
            parsed_obj = NameParser(series=obj.series).parse(new_nzb)
        except (InvalidNameException, InvalidShowException) as error:
            log.debug(u'{}', error)
            return []

        # make sure the result is sane
        if (parsed_obj.season_number != season) or (parsed_obj.season_number is None and season != 1):
            # pylint: disable=no-member
            log.warning(u'Found {new_nzb} inside {name} but it doesn\'t seem to belong to the same season, ignoring it',
                        {'new_nzb': new_nzb, 'name': obj.name})
            continue
        elif not parsed_obj.episode_numbers:
            # pylint: disable=no-member
            log.warning(u'Found {new_nzb} inside {name} but it doesn\'t seem to be a valid episode NZB, ignoring it',
                        {'new_nzb': new_nzb, 'name': obj.name})
            continue

        want_ep = True
        for ep_num in parsed_obj.episode_numbers:
            if not obj.extraInfo[0].want_episode(season, ep_num, obj.quality):
                log.debug(u'Ignoring result: {0}', new_nzb)
                want_ep = False
                break
        if not want_ep:
            continue

        # get all the associated episode objects
        ep_obj_list = [obj.extraInfo[0].get_episode(season, ep) for ep in parsed_obj.episode_numbers]

        # make a result
        cur_obj = NZBDataSearchResult(ep_obj_list)
        cur_obj.name = new_nzb
        cur_obj.provider = obj.provider
        cur_obj.quality = obj.quality
        cur_obj.extraInfo = [create_nzb_string(separate_nzbs[new_nzb], xmlns)]

        result_list.append(cur_obj)

    return result_list
