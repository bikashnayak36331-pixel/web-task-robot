import http.cookiejar
import urllib.error
import urllib.parse
import urllib.request


def opener():
    return urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()), urllib.request.ProxyHandler({})
    )


def post(op, url, data):
    return op.open(url, urllib.parse.urlencode(data).encode())


def test_login_validation_and_pages(site):
    op = opener()
    assert "Sign in" in op.open(site.url + "/customers/new").read().decode()  # redirected to login
    try:
        post(op, site.url + "/login", {"email": site.state.user, "password": "wrong"})
        raise AssertionError("expected 401")
    except urllib.error.HTTPError as e:
        assert e.code == 401
    assert (
        "Add customer"
        in post(op, site.url + "/login", {"email": site.state.user, "password": site.state.password}).read().decode()
    )
    try:
        post(op, site.url + "/customers/new", {"name": "X", "email": "bad", "plan": "Pro"})
        raise AssertionError("expected 422")
    except urllib.error.HTTPError as e:
        assert e.code == 422 and "not a valid email" in e.read().decode()
    ok = post(op, site.url + "/customers/new", {"name": "Ana", "email": "ana@x.example", "plan": "Pro"}).read().decode()
    assert "CUS-1002" in ok
    page4 = op.open(site.url + "/products?page=4").read().decode()
    assert page4.count('class="product-card"') == 6 and 'class="next"' not in page4
