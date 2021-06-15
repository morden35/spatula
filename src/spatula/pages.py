import io
import csv
import tempfile
import subprocess
import logging
import typing
import scrapelib
import lxml.html  # type: ignore
from openpyxl import load_workbook  # type: ignore
from .sources import Source, URL


class MissingSourceError(Exception):
    def __init__(self, msg: str):
        super().__init__(msg)


class HandledError(Exception):
    def __init__(self, exc: Exception):
        super().__init__(exc)


class Page:
    """
    Base class for all *Page* scrapers, used for scraping information from a single type of page.

    **Attributes**

    `source`
    :   Can be set on subclasses of `Page` to define the initial HTTP request
        that the page will handle in its `process_response` method.

        For simple GET requests, `source` can be a string.
        `URL` should be used for more advanced use cases.

    `response`
    :   [`requests.Response`](https://docs.python-requests.org/en/master/api/#requests.Response)
        object available if access is needed to the raw response for any reason.

    `input`
    :   Instance of data being passed upon instantiation of this page.
        Must be of type `input_type`.

    `input_type`
    :   `dataclass`, `attrs` class, or `pydantic` model.
        If set will be used to prompt for and/or validate `self.input`

    `example_input`
    :   Instance of `input_type` to be used when invoking `spatula test`.

    `example_source`
    :   Source to fetch when invokking `spatula test`.

    `dependencies`
    :   TODO: document

    **Methods**
    """

    source: typing.Union[None, str, Source] = None
    dependencies: typing.Dict[str, "Page"] = {}

    def _fetch_data(self, scraper: scrapelib.Scraper) -> None:
        """
        ensure that the page has all of its data, this is guaranteed to be called
        exactly once before process_page is invoked
        """
        # process dependencies first
        for val, dep in self.dependencies.items():
            if isinstance(dep, type):
                dep = dep(self.input)
            dep._fetch_data(scraper)
            setattr(self, val, dep.process_page())

        if not self.source:
            try:
                self.source = self.get_source_from_input()
            except NotImplementedError:
                raise MissingSourceError(
                    f"{self.__class__.__name__} has no source or get_source_from_input"
                )
        if isinstance(self.source, str):
            self.source = URL(self.source)
        # at this point self.source is indeed a Source
        self.logger.info(f"fetching {self.source}")
        try:
            self.response = self.source.get_response(scraper)  # type: ignore
            if getattr(self.response, "fromcache", None):
                self.logger.debug(f"retrieved {self.source} from cache")
        except scrapelib.HTTPError as e:
            self.process_error_response(e)
            raise HandledError(e)
        else:
            self.postprocess_response()

    def __init__(
        self,
        input_val: typing.Any = None,
        *,
        source: typing.Union[None, str, Source] = None,
    ):
        self.input = input_val
        # allow possibility to override default source, useful during dev
        if source:
            self.source = source
        self.logger = logging.getLogger(
            self.__class__.__module__ + "." + self.__class__.__name__
        )

    def __str__(self) -> str:
        s = f"{self.__class__.__name__}("
        if self.input:
            s += f"input={self.input} "
        if self.source:
            s += f"source={self.source}"
        s += ")"
        return s

    def get_source_from_input(self) -> typing.Union[None, str, Source]:
        """
        To be overridden.

        Convert `self.input` to a `Source` object such as a `URL`.
        """
        raise NotImplementedError()

    def postprocess_response(self) -> None:
        """
        To be overridden.

        This is called after source.get_response but before self.process_page.
        """
        pass

    def process_error_response(self, exception: Exception) -> None:
        """
        To be overridden.

        This is called after source.get_response if an exception is raised.
        """
        raise exception

    def process_page(self) -> typing.Any:
        """
        To be overridden.

        Return data extracted from this page and this page alone.
        """
        raise NotImplementedError()

    def get_next_source(self) -> typing.Union[None, str, Source]:
        """
        To be overriden for paginated pages.

        Return a URL or valid source to fetch the next page, None if there isn't one.
        """
        return None


class HtmlPage(Page):
    """
    Page that automatically handles parsing and normalizing links in an HTML response.

    **Attributes**

    `root`
    :   [`lxml.etree.Element`](https://lxml.de/api/lxml.etree._Element-class.html)
    object representing the root element (e.g. `<html>`) on the page.

        Can use the normal lxml methods (such as `cssselect` and `getchildren`), or
        use this element as the target of a `Selector` subclass.
    """

    def postprocess_response(self) -> None:
        self.root = lxml.html.fromstring(self.response.content)
        if hasattr(self.source, "url"):
            self.root.make_links_absolute(self.source.url)  # type: ignore


class XmlPage(Page):
    """
    Page that automatically handles parsing a XML response.

    **Attributes**

    `root`
    :   [`lxml.etree.Element`](https://lxml.de/api/lxml.etree._Element-class.html)
    object representing the root XML element on the page.
    """

    def postprocess_response(self) -> None:
        self.root = lxml.etree.fromstring(self.response.content)


class JsonPage(Page):
    """
    Page that automatically handles parsing a JSON response.

    **Attributes**

    `data`
    :   JSON data from response.  (same as `self.response.json()`)
    """

    def postprocess_response(self) -> None:
        self.data = self.response.json()


class PdfPage(Page):  # pragma: no cover
    """
    Page that automatically handles converting a PDF response to text using `pdftotext`.

    **Attributes**

    `preserve_layout`
    :   set to `True` on derived class if you want the conversion function to use pdftotext's
        -layout option to attempt to preserve the layout of text.
        (`False` by default)

    `text`
    :   UTF8 text extracted by pdftotext.
    """

    preserve_layout = False

    def postprocess_response(self) -> None:
        with tempfile.NamedTemporaryFile() as temp:
            temp.write(self.response.content)
            temp.flush()
            temp.seek(0)

            if self.preserve_layout:
                command = ["pdftotext", "-layout", temp.name, "-"]
            else:
                command = ["pdftotext", temp.name, "-"]

            try:
                pipe = subprocess.Popen(
                    command, stdout=subprocess.PIPE, close_fds=True
                ).stdout
            except OSError as e:
                raise EnvironmentError(
                    f"error running pdftotext, missing executable? [{e}]"
                )
            if pipe is None:
                raise EnvironmentError("no stdout from pdftotext")
            else:
                data = pipe.read()
                pipe.close()
        self.text = data.decode("utf8")


class ListPage(Page):
    """
    Base class for common pattern of extracting many homogenous items from one page.

    Instead of overriding `process_response`, subclasses should provide a `process_item`.

    **Methods**
    """

    class SkipItem(Exception):
        def __init__(self, msg: str):
            super().__init__(msg)

    def skip(self, msg: str = "") -> None:
        """
        Can be called from within `process_item` to skip a given item.

        Typically used if there is some known bad data.
        """
        raise self.SkipItem(msg)

    def _process_or_skip_loop(
        self, iterable: typing.Iterable
    ) -> typing.Iterable[typing.Any]:
        for item in iterable:
            try:
                item = self.process_item(item)
            except self.SkipItem as e:
                self.logger.debug(f"SkipItem: {e}")
                continue
            yield item

    def process_item(self, item: typing.Any) -> typing.Any:
        """
        To be overridden.

        Called once per subitem on page, as defined by the particular subclass being used.
        """
        return item


class CsvListPage(ListPage):
    """
    Processes each row in a CSV (after the first, assumed to be headers) as an item
    with `process_item`.
    """

    def postprocess_response(self) -> None:
        self.reader = csv.DictReader(io.StringIO(self.response.text))

    def process_page(self) -> typing.Iterable[typing.Any]:
        yield from self._process_or_skip_loop(self.reader)


class ExcelListPage(ListPage):
    """
    Processes each row in an Excel file as an item with `process_item`.
    """

    def postprocess_response(self) -> None:
        workbook = load_workbook(io.BytesIO(self.response.content))
        # TODO: allow selecting this with a class property
        self.worksheet = workbook.active

    def process_page(self) -> typing.Iterable[typing.Any]:
        yield from self._process_or_skip_loop(self.worksheet.values)


class LxmlListPage(ListPage):
    """
    Base class for XML and HTML subclasses below, only difference is which
    parser is used.

    Simplification for pages that get a list of items and process them.

    When overriding the class, instead of providing process_page, one must only provide
    a selector and a process_item function.
    """

    selector = None

    def process_page(self) -> typing.Iterable[typing.Any]:
        if not self.selector:
            raise NotImplementedError("must either provide selector or override scrape")
        items = self.selector.match(self.root)
        yield from self._process_or_skip_loop(items)


class HtmlListPage(LxmlListPage, HtmlPage):
    """
    Selects homogenous items from HTML page using `selector` and passes them to `process_item`.

    **Attributes**

    `selector`
    :   `Selector` subclass which matches list of homogenous elements to process.  (e.g. `CSS("tbody tr")`)
    """

    pass


class XmlListPage(LxmlListPage, XmlPage):
    """
    Selects homogenous items from XML document using `selector` and passes them to `process_item`.

    **Attributes**

    `selector`
    :   `Selector` subclass which matches list of homogenous elements to process.  (e.g. `XPath("//item")`)
    """

    pass


class JsonListPage(ListPage, JsonPage):
    """
    Processes each element in a JSON list as an item with `process_item`.
    """

    def process_page(self) -> typing.Iterable[typing.Any]:
        yield from self._process_or_skip_loop(self.data)
