import ManualTrackForm from "./ManualTrackForm";
import { useCallback, useEffect, useState } from "react";
import axios from "axios";
import "./App.css";

const API = import.meta.env.VITE_API_URL || "http://localhost:8000";

const RETAILERS = [
  {
    name: "Amazon",
    subtitle: "Electronics, fashion & more",
    initials: "a",
    className: "amazon",
    url: "https://www.amazon.in/",
  },
  {
    name: "Flipkart",
    subtitle: "Mobiles, laptops & appliances",
    initials: "F",
    className: "flipkart",
    url: "https://www.flipkart.com/",
  },
  {
    name: "Croma",
    subtitle: "Technology & electronics",
    initials: "C",
    className: "croma",
    url: "https://www.croma.com/",
  },
  {
    name: "Reliance Digital",
    subtitle: "Electronics & gadgets",
    initials: "R",
    className: "reliance",
    url: "https://www.reliancedigital.in/",
  },
];

const PLACEHOLDER_IMAGE =
  "https://placehold.co/600x400/171725/c4ff4d?text=Product";



function money(value, currency = "INR") {
  const amount = Number(value);

  if (
    value === null ||
    value === undefined ||
    value === "" ||
    !Number.isFinite(amount)
  ) {
    return "Price unavailable";
  }

  try {
    return new Intl.NumberFormat("en-IN", {
      style: "currency",
      currency,
      maximumFractionDigits: 2,
    }).format(amount);
  } catch {
    return `₹${amount.toLocaleString("en-IN")}`;
  }
}

function productMoney(value, currency = "INR") {
  const amount = Number(value);

  if (
    value === null ||
    value === undefined ||
    value === "" ||
    !Number.isFinite(amount) ||
    amount <= 0
  ) {
    return "Price unavailable";
  }

  return money(amount, currency);
}



function getErrorMessage(error) {
  const detail = error?.response?.data?.detail;

  if (typeof detail === "string") return detail;

  if (Array.isArray(detail)) {
    return detail.map((item) => item.msg).join(", ");
  }

  return (
    error?.response?.data?.message ||
    error?.message ||
    "Something went wrong."
  );
}

function getProductImage(product) {
  return product?.image_url || product?.image || PLACEHOLDER_IMAGE;
}

function getRetailerName(product) {
  return (
    product?.retailer ||
    product?.source ||
    "Retailer not specified"
  );
}

function getProductPrice(product) {
  return product?.price ?? product?.current_price ?? null;
}

function isExternalProduct(product) {
  return product?.source === "Google Shopping via SerpApi";
}

function getPriceLabel(product) {
  if (isExternalProduct(product)) {
    return product.price_status === "provider_reported"
      ? "PROVIDER-REPORTED PRICE"
      : "PRICE UNAVAILABLE";
  }

  return product?.price_status === "verified"
    ? "VERIFIED PRICE"
    : "CATALOG LISTING";
}

const DISCOUNT_STATUS_COPY = {
  insufficient_history: "NOT ENOUGH HISTORY",
  unverified_claim: "CLAIM NOT VERIFIED",
  suspicious_pre_sale_increase: "POSSIBLE PRE-SALE INCREASE",
  consistent_with_history: "CONSISTENT WITH HISTORY",
};

function DiscountEvidence({ detector, currency = "INR" }) {
  if (!detector?.has_claim) return null;

  const history = detector.history || {};
  const status = detector.status || "unverified_claim";
  const label = DISCOUNT_STATUS_COPY[status] || "CLAIM NOT VERIFIED";
  const severity = detector.severity || "neutral";
  const hasHistory = Number(history.observation_count) > 0;

  return (
    <aside
      className={`discount-evidence discount-evidence-${severity}`}
      aria-label={`Discount check: ${label.toLowerCase()}`}
    >
      <div className="discount-evidence-heading">
        <span className="discount-evidence-label">DISCOUNT CHECK</span>
        <strong>{label}</strong>
      </div>

      <p>{detector.message}</p>

      <div className="discount-evidence-stats">
        <span>
          <small>CLAIMED</small>
          <strong>{detector.claimed_discount_percent}% off</strong>
        </span>
        <span>
          <small>WAS</small>
          <strong>{productMoney(detector.claimed_original_price, currency)}</strong>
        </span>
        <span>
          <small>NOW</small>
          <strong>{productMoney(detector.current_price, currency)}</strong>
        </span>
        {hasHistory && (
          <span>
            <small>HISTORY MEDIAN</small>
            <strong>{productMoney(history.median_price, currency)}</strong>
          </span>
        )}
        {hasHistory && (
          <span>
            <small>HISTORY LOW</small>
            <strong>{productMoney(history.lowest_price, currency)}</strong>
          </span>
        )}
      </div>

      <span className="discount-evidence-source">
        {hasHistory
          ? `${history.observation_count} recorded checks across ${history.days_spanned} day${history.days_spanned === 1 ? "" : "s"}.`
          : "No recorded checks for this exact retailer listing yet."}
      </span>
    </aside>
  );
}

export default function App() {
  const [page, setPage] = useState("dashboard");

  const [theme, setTheme] = useState(
    () => localStorage.getItem("theme") || "dark"
  );

  const [token, setToken] = useState(
    () => localStorage.getItem("access_token") || ""
  );

  const [user, setUser] = useState(
    () => localStorage.getItem("user_email") || ""
  );

  const [query, setQuery] = useState("");
  const [searchResults, setSearchResults] = useState([]);
  const [searchMode, setSearchMode] = useState("external");
  const [watchlist, setWatchlist] = useState([]);

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [authMode, setAuthMode] = useState("login");

  const [searchLoading, setSearchLoading] = useState(false);
  const [authLoading, setAuthLoading] = useState(false);
  const [watchlistLoading, setWatchlistLoading] = useState(false);
  const [trackingId, setTrackingId] = useState(null);

  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false);

  const authHeaders = useCallback(() => {
    return token
      ? { Authorization: `Bearer ${token}` }
      : {};
  }, [token]);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    localStorage.setItem("theme", theme);
  }, [theme]);

  // Validate the saved login when the application starts.
  useEffect(() => {
    if (!token) return;

    let cancelled = false;

    axios
      .get(`${API}/api/auth/me`, {
        headers: { Authorization: `Bearer ${token}` },
      })
      .then((response) => {
        if (cancelled) return;

        const profile = response.data?.user || response.data;
        const emailAddress = profile?.email || user;

        setUser(emailAddress);
        localStorage.setItem("user_email", emailAddress);
      })
      .catch((err) => {
        if (cancelled) return;

        const status = err?.response?.status;
        // A network hiccup or server error must not log the user out.
        if (status !== 401 && status !== 403) return;

        setToken("");
        setUser("");
        localStorage.removeItem("access_token");
        localStorage.removeItem("user_email");
      });

    return () => {
      cancelled = true;
    };
  }, []);

  const loadWatchlist = useCallback(async () => {
    if (!token) {
      setWatchlist([]);
      return;
    }

    setWatchlistLoading(true);

    try {
      const response = await axios.get(
        `${API}/api/products/watchlist`,
        { headers: authHeaders() }
      );

      setWatchlist(
        Array.isArray(response.data) ? response.data : []
      );
    } catch (err) {
      setError(
        `Could not load your tracked products. ${getErrorMessage(err)}`
      );
    } finally {
      setWatchlistLoading(false);
    }
  }, [token, authHeaders]);

  useEffect(() => {
    if (token) {
      loadWatchlist();
    } else {
      setWatchlist([]);
    }
  }, [token, loadWatchlist]);

  const navigate = (nextPage) => {
    setPage(nextPage);
    setError("");
    setMessage("");
    setMobileMenuOpen(false);

    if (nextPage === "watchlist" && !token) {
      setPage("profile");
      setMessage(
        "Log in to see your personal tracked-products list."
      );
    }
  };

  // External mode calls SerpApi through the backend.
  // Catalog mode searches products already saved in PostgreSQL.
  const handleSearch = async (event) => {
    event?.preventDefault();

    const searchTerm = query.trim();

    if (searchTerm.length < 2) {
      setError("Enter at least two characters to search.");
      return;
    }

    setSearchLoading(true);
    setError("");
    setMessage("");
    setPage("search");

    try {
      if (searchMode === "external") {
        const response = await axios.get(
          `${API}/api/products/compare/external`,
          { params: { q: searchTerm, limit: 20 } }
        );

        setSearchResults(
          Array.isArray(response.data?.groups)
            ? response.data.groups
            : []
        );
      } else {
        const response = await axios.get(
          `${API}/api/products/search`,
          {
            params: {
              q: searchTerm,
              limit: 50,
            },
          }
        );

        setSearchResults(
          Array.isArray(response.data) ? response.data : []
        );
      }
    } catch (err) {
      setSearchResults([]);
      setError(`Search failed. ${getErrorMessage(err)}`);
    } finally {
      setSearchLoading(false);
    }
  };

  const handleAuth = async (event) => {
    event.preventDefault();
    setError("");
    setMessage("");

    if (!email.trim() || !password) {
      setError("Enter your email and password.");
      return;
    }

    setAuthLoading(true);

    const endpoint =
      authMode === "register"
        ? "/api/auth/register"
        : "/api/auth/login";

    try {
      const response = await axios.post(`${API}${endpoint}`, {
        email: email.trim(),
        password,
      });

      const accessToken = response.data?.access_token;
      const accountEmail =
        response.data?.user?.email ||
        response.data?.email ||
        email.trim();

      if (!accessToken) {
        throw new Error(
          "The server did not return an access token. Check the authentication API response."
        );
      }

      setToken(accessToken);
      setUser(accountEmail);

      localStorage.setItem("access_token", accessToken);
      localStorage.setItem("user_email", accountEmail);

      setPassword("");
      setPage("profile");

      setMessage(
        authMode === "register"
          ? "Account created successfully!"
          : "Welcome back! You are logged in."
      );
    } catch (err) {
      setError(getErrorMessage(err));
    } finally {
      setAuthLoading(false);
    }
  };

  const handleLogout = () => {
    setToken("");
    setUser("");
    setWatchlist([]);

    localStorage.removeItem("access_token");
    localStorage.removeItem("user_email");

    setPage("dashboard");
    setMessage("You have been logged out.");
    setError("");
  };

  // Saved-catalog products have database IDs and can be tracked.
  // External search results must first be saved through a backend
  // endpoint before they can be added to the watchlist.
  const trackProduct = async (product) => {
    if (!token) {
      setPage("profile");
      setMessage("Log in first to save products to your watchlist.");
      return;
    }

    if (product?.offers && product.offers.length) {
      const cheapest = product.offers.find((offer) => Number(offer.price) > 0);
      if (!cheapest) {
        setError("This product has no usable price to track yet.");
        return;
      }
      setTrackingId(product.title);
      setError("");
      setMessage("");
      try {
        const response = await axios.post(
          `${API}/api/products/track-external`,
          {
            title: product.title,
            url: cheapest.url,
            retailer: cheapest.retailer,
            price: cheapest.price,
            image_url: cheapest.image_url || product.image_url || undefined,
          },
          { headers: authHeaders() }
        );
        setMessage(response.data?.message || "Product added to your watchlist.");
        await loadWatchlist();
      } catch (err) {
        setError(`Could not track this product. ${getErrorMessage(err)}`);
      } finally {
        setTrackingId(null);
      }
      return;
    }

    if (isExternalProduct(product)) {
      setError("Choose a product comparison card before tracking.");
      return;
    }

    if (!product?.id) {
      setError(
        "This product has no database ID and cannot be tracked yet."
      );
      return;
    }

    setTrackingId(product.id);
    setError("");
    setMessage("");

    try {
      const response = await axios.post(
        `${API}/api/products/${product.id}/track`,
        {},
        { headers: authHeaders() }
      );

      setMessage(
        response.data?.message ||
          "Product added to your watchlist."
      );

      await loadWatchlist();
    } catch (err) {
      setError(
        `Could not track this product. ${getErrorMessage(err)}`
      );
    } finally {
      setTrackingId(null);
    }
  };

  const removeTrackedProduct = async (subscriptionId) => {
    if (!token) return;

    setError("");
    setMessage("");

    try {
      await axios.delete(
        `${API}/api/products/watchlist/${subscriptionId}`,
        { headers: authHeaders() }
      );

      setWatchlist((current) =>
        current.filter(
          (item) => item.subscription_id !== subscriptionId
        )
      );

      setMessage("Product removed from your tracked list.");
    } catch (err) {
      setError(
        `Could not remove the product. ${getErrorMessage(err)}`
      );
    }
  };

  const toggleTheme = () => {
    setTheme((current) =>
      current === "dark" ? "light" : "dark"
    );
  };

  const navItems = [
    { id: "dashboard", icon: "⌂", label: "Dashboard" },
    { id: "search", icon: "⌕", label: "Search & Compare" },
    { id: "watchlist", icon: "♡", label: "Tracked Products" },
    { id: "budget", icon: "▣", label: "Budget Basket" },
    { id: "profile", icon: "◉", label: "Profile & Settings" },
  ];

  const currentTitle =
    navItems.find((item) => item.id === page)?.label ||
    "Dashboard";

  return (
    <div className="app-container">
      <aside
        className={`sidebar ${mobileMenuOpen ? "sidebar-open" : ""}`}
      >
<button
  className="brand"
  onClick={() => navigate("dashboard")}
  type="button"
  aria-label="PricePulse home"
>
  <img
    className="brand-icon"
    src="/pricepulse-icon.png"
    alt=""
  />
  <span className="brand-copy">
    <span className="brand-name">
      Price<span className="brand-accent">Pulse</span>
    </span>
    <small>SHOP SMARTER.</small>
  </span>
</button>

        <div className="sidebar-label">WORKSPACE</div>

        <nav className="nav-links">
          {navItems.map((item) => (
            <button
              type="button"
              key={item.id}
              className={page === item.id ? "active" : ""}
              onClick={() => navigate(item.id)}
            >
              <span className="nav-icon">{item.icon}</span>
              <span>{item.label}</span>

              {item.id === "watchlist" &&
                watchlist.length > 0 && (
                  <span className="nav-count">
                    {watchlist.length}
                  </span>
                )}
            </button>
          ))}
        </nav>

        <div className="sidebar-promo">
          <span className="promo-spark">✳</span>
          <h3>Buy smarter.</h3>
          <p>Compare what is available before you spend.</p>
          <button
            type="button"
            onClick={() => navigate("search")}
          >
            Find a product <span>↗</span>
          </button>
        </div>

        <div className="sidebar-bottom">
          <button
            className="theme-toggle"
            onClick={toggleTheme}
            type="button"
          >
            <span>{theme === "dark" ? "☀" : "☾"}</span>
            <span>
              {theme === "dark" ? "Switch to light" : "Switch to dark"}
            </span>
            <span className="theme-state">
              {theme === "dark" ? "DARK" : "LIGHT"}
            </span>
          </button>

          <button
            className="sidebar-user"
            onClick={() => navigate("profile")}
            type="button"
          >
            <span className="avatar">
              {user ? user.charAt(0).toUpperCase() : "G"}
            </span>
            <span className="sidebar-user-copy">
              <strong>
                {user ? user.split("@")[0] : "Guest account"}
              </strong>
              <small>{user || "Log in to get started"}</small>
            </span>
            <span className="user-arrow">↗</span>
          </button>
        </div>
      </aside>

      {mobileMenuOpen && (
        <button
          className="mobile-backdrop"
          aria-label="Close navigation"
          onClick={() => setMobileMenuOpen(false)}
        />
      )}

      <main className="main-content">
        <header className="topbar">
          <div className="topbar-title">
            <button
              className="mobile-menu-button"
              onClick={() => setMobileMenuOpen((open) => !open)}
              type="button"
              aria-label="Toggle navigation"
            >
              ☰
            </button>

            <div>
              <span className="breadcrumb">PRICEPULSE /</span>
              <h1>{currentTitle}</h1>
            </div>
          </div>

          <form className="search-bar" onSubmit={handleSearch}>
            <span>⌕</span>
            <input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Search products..."
              aria-label="Search products"
            />
            <kbd>↵</kbd>
          </form>

          <div className="topbar-actions">
            <span className={`status-dot ${token ? "online" : ""}`} />
            <span className="session-label">
              {token ? "Account active" : "Guest mode"}
            </span>
            <button
              className="top-avatar"
              onClick={() => navigate("profile")}
              type="button"
              aria-label="Open profile"
            >
              {user ? user.charAt(0).toUpperCase() : "G"}
            </button>
          </div>
        </header>

        <div className="content-body">
          {message && (
            <div className="notice notice-success">
              <span>✓</span>
              <p>{message}</p>
              <button
                onClick={() => setMessage("")}
                type="button"
              >
                ×
              </button>
            </div>
          )}

          {error && (
            <div className="notice notice-error">
              <span>!</span>
              <p>{error}</p>
              <button
                onClick={() => setError("")}
                type="button"
              >
                ×
              </button>
            </div>
          )}

          {page === "dashboard" && (
            <Dashboard
              user={user}
              watchlist={watchlist}
              onNavigate={navigate}
            />
          )}

          {page === "search" && (
            <SearchPage
              query={query}
              setQuery={setQuery}
              onSearch={handleSearch}
              results={searchResults}
              loading={searchLoading}
              searchMode={searchMode}
              setSearchMode={(mode) => {
                setSearchMode(mode);
                setSearchResults([]);
              }}
              onTrack={trackProduct}
              trackingId={trackingId}
              onNavigate={navigate}
            />
          )}

          {page === "watchlist" && (
  <>
    <ManualTrackForm
      token={token}
      onSuccess={loadWatchlist}
    />

    <WatchlistPage
      items={watchlist}
      loading={watchlistLoading}
      onRemove={removeTrackedProduct}
      onNavigate={navigate}
      token={token}
      onNotify={setMessage}
      onError={setError}
    />
  </>
)}

{page === "budget" && (
  <BudgetBasket watchlist={watchlist} />
)}


          {page === "profile" && (
            <ProfilePage
              user={user}
              email={email}
              setEmail={setEmail}
              password={password}
              setPassword={setPassword}
              authMode={authMode}
              setAuthMode={setAuthMode}
              loading={authLoading}
              onSubmit={handleAuth}
              onLogout={handleLogout}
              onNavigate={navigate}
            />
          )}

          <footer className="app-footer">
            <span>MADE FOR SMARTER SHOPPING ✳</span>
            <span>
              PricePulse <span className="footer-dot">●</span>{" "}
              Compare before you buy.
            </span>
          </footer>
        </div>
      </main>
    </div>
  );
}

function Dashboard({ user, watchlist, onNavigate }) {
  return (
    <div className="page-stack">
      <section className="hero">
        <div className="hero-copy">
          <span className="eyebrow">
            <span className="live-pulse" /> YOUR SMART SHOPPING SPACE
          </span>
          <h2>
            Less scrolling.
            <br />
            <span>More saving.</span>
          </h2>
          <p>
            Find a product, compare available prices, and keep an eye
            on the things you want to buy.
          </p>
          <button
            className="btn-primary hero-button"
            onClick={() => onNavigate("search")}
            type="button"
          >
            Explore products <span>↗</span>
          </button>
          <div className="hero-footnote">
            <span>✳</span> One place for your shopping research
          </div>
        </div>

        <div className="hero-art" aria-hidden="true">
          <div className="hero-ring ring-one" />
          <div className="hero-ring ring-two" />
          <div className="hero-orb">₹</div>
          <div className="floating-tag tag-top">
            <span className="tag-icon">↗</span>
            <span>
              <small>SHOPPING MODE</small>
              <strong>ON POINT.</strong>
            </span>
          </div>
          <div className="floating-tag tag-bottom">
            <span className="tag-check">✓</span>
            <span>
              <small>YOUR NEXT PURCHASE</small>
              <strong>Make it count.</strong>
            </span>
          </div>
          <div className="hero-star star-one">✳</div>
          <div className="hero-star star-two">✦</div>
        </div>
      </section>

      <section className="stats-grid stats-grid-4">
        <StatCard
          icon="⌕"
          label="PRODUCT SEARCH"
          value="Explore"
          detail="Search retailer listings"
        />
        <StatCard
          icon="♡"
          label="YOUR WATCHLIST"
          value={watchlist.length}
          detail="Products you are tracking"
        />
        <StatCard
          icon="↗"
          label="RETAILER OPTIONS"
          value="04"
          detail="Retailer websites to explore"
        />
        <StatCard
          icon="◉"
          label="ACCOUNT"
          value={user ? "Ready" : "Guest"}
          detail={user ? "Your account is active" : "Log in to save products"}
        />
      </section>

      <section className="section-block">
        <div className="section-heading">
          <div>
            <span className="eyebrow">THE SHOPPING LINEUP</span>
            <h2>
              Retailers to explore <span>↗</span>
            </h2>
            <p>Visit retailer websites to check their current offers.</p>
          </div>
          <span className="section-index">01 / RETAILERS</span>
        </div>

        <div className="retailer-grid">
          {RETAILERS.map((retailer, index) => (
            <a
              className="retailer-card"
              href={retailer.url}
              target="_blank"
              rel="noreferrer"
              key={retailer.name}
            >
              <div className={`retailer-mark ${retailer.className}`}>
                {retailer.initials}
              </div>
              <div className="retailer-info">
                <span className="retailer-number">0{index + 1}</span>
                <h3>{retailer.name}</h3>
                <p>{retailer.subtitle}</p>
                <span className="retailer-link">Visit website ↗</span>
              </div>
              <span className="retailer-arrow">↗</span>
            </a>
          ))}
        </div>
      </section>

      <section className="bottom-banner">
        <div className="banner-symbol">✳</div>
        <div>
          <span className="eyebrow">YOUR NEXT GOOD BUY STARTS HERE</span>
          <h2>
            {user
              ? "Ready to compare something?"
              : "Your wishlist deserves a plan."}
          </h2>
          <p>
            {user
              ? "Search products and add catalog items to your personal watchlist."
              : "Create an account to save products and build your own tracked list."}
          </p>
        </div>
        <button
          className="btn-primary"
          onClick={() => onNavigate(user ? "search" : "profile")}
          type="button"
        >
          {user ? "Start comparing" : "Get started"} <span>↗</span>
        </button>
      </section>

      <p className="disclaimer">
        Retailer links open external websites. Prices and availability can change;
        verify current offers directly with the retailer.
      </p>
    </div>
  );
}

function StatCard({ icon, label, value, detail }) {
  return (
    <div className="stat-card">
      <div className="stat-top">
        <span className="stat-icon">{icon}</span>
        <span className="stat-label">{label}</span>
      </div>
      <strong className="stat-value">{value}</strong>
      <span className="stat-detail">{detail}</span>
    </div>
  );
}

function SearchPage({
  query,
  setQuery,
  onSearch,
  results,
  loading,
  searchMode,
  setSearchMode,
  onTrack,
  trackingId,
  onNavigate,
}) {
  return (
    <div className="page-stack">
      <section className="page-intro">
        <span className="eyebrow">FIND YOUR NEXT BUY</span>
        <h2>
          Search. Compare. <span>Decide.</span>
        </h2>
        <p>
          Compare provider-reported retailer listings or search your saved catalog.
        </p>
      </section>

      <div className="auth-tabs">
        <button
          type="button"
          className={searchMode === "external" ? "selected" : ""}
          onClick={() => setSearchMode("external")}
        >
          Indian retailer search
        </button>
        <button
          type="button"
          className={searchMode === "catalog" ? "selected" : ""}
          onClick={() => setSearchMode("catalog")}
        >
          Saved catalog
        </button>
      </div>

      <form className="large-search" onSubmit={onSearch}>
        <span>⌕</span>
        <input
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="Try Acer Nitro V RTX 4050..."
          aria-label="Product search"
        />
        <button className="btn-primary" disabled={loading} type="submit">
          {loading ? "Searching..." : "Search products"}
        </button>
      </form>

      <div className="results-heading">
        <div>
          <h3>{results.length ? "Available results" : "Your search results"}</h3>
          <p>
            {results.length
              ? `${results.length} listing${results.length === 1 ? "" : "s"} found`
              : searchMode === "external"
                ? "Search Google Shopping through the configured backend provider."
                : "Search products already stored in your catalog."}
          </p>
        </div>
        <span className="data-chip">
          {searchMode === "external" ? "EXTERNAL LISTINGS" : "CATALOG DATA"}
        </span>
      </div>

      {loading ? (
        <div className="empty-state">
          <span className="loader" /> Searching...
        </div>
      ) : results.length === 0 ? (
        <div className="empty-state">
          <div className="empty-icon">⌕</div>
          <h3>Nothing to compare just yet</h3>
          <p>
            Try a different search phrase. External results depend on the
            search provider; catalog results depend on saved database records.
          </p>
          <button
            className="btn-secondary"
            onClick={() => onNavigate("dashboard")}
            type="button"
          >
            Back to dashboard
          </button>
        </div>
      ) : (
        <div className="product-grid">
          {results.map((product, index) => {
            
if (Array.isArray(product.offers)) {
  const offers = product.offers;

  // Sort priced offers from lowest to highest.
  const sortedOffers = [...offers].sort((a, b) => {
    const priceA = Number(a.price);
    const priceB = Number(b.price);
    const validA = Number.isFinite(priceA) && priceA > 0;
    const validB = Number.isFinite(priceB) && priceB > 0;

    if (validA && validB) return priceA - priceB;
    if (validA) return -1;
    if (validB) return 1;
    return 0;
  });

  const cheapest = sortedOffers.find(
    (offer) =>
      Number.isFinite(Number(offer.price)) &&
      Number(offer.price) > 0
  );

  const image =
    offers.find((offer) => offer.image_url)?.image_url ||
    product.image_url ||
    PLACEHOLDER_IMAGE;

  const pricedOffers = sortedOffers.filter(
    (offer) =>
      Number.isFinite(Number(offer.price)) &&
      Number(offer.price) > 0
  );

  const highestPrice =
    pricedOffers.length > 0
      ? Number(pricedOffers[pricedOffers.length - 1].price)
      : null;

  const savings =
    cheapest && highestPrice !== null
      ? highestPrice - Number(cheapest.price)
      : 0;

  return (
    <article
      className="product-card comparison-card"
      key={`${product.title}-${index}`}
    >
      <div className="product-image-wrap">
        <img
          className="product-image"
          src={image}
          alt={product.title || "Product"}
          onError={(event) => {
            event.currentTarget.onerror = null;
            event.currentTarget.src = PLACEHOLDER_IMAGE;
          }}
        />
        <span className="price-status">PRICE COMPARISON</span>
      </div>

      <div className="product-info">
        <span className="product-retailer">
          {pricedOffers.length} priced offer
          {pricedOffers.length === 1 ? "" : "s"}
          {" · "}
          {offers.length} total listing
          {offers.length === 1 ? "" : "s"}
        </span>

        <h3>{product.title}</h3>

        <div className="comparison-lowest">
          <span className="comparison-label">LOWEST REPORTED PRICE</span>
          <div className="product-price">
            {cheapest ? money(cheapest.price) : "Price unavailable"}
          </div>

          {cheapest && (
            <span className="comparison-winner">
              Lowest listed offer: {cheapest.retailer}
            </span>
          )}

          {pricedOffers.length > 1 && savings > 0 && (
            <span className="comparison-savings">
              Difference between highest and lowest: {money(savings)}
            </span>
          )}
        </div>

        <div className="offer-list">
          <div className="offer-list-heading">
            <span>RETAILER</span>
            <span>PRICE</span>
            <span>LINK</span>
          </div>

          {sortedOffers.map((offer, offerIndex) => {
            const price = Number(offer.price);
            const hasPrice =
              Number.isFinite(price) && price > 0;

            const isCheapest =
              hasPrice &&
              cheapest &&
              price === Number(cheapest.price);

            return (
              <div
                className={`offer-row ${
                  isCheapest ? "offer-row-cheapest" : ""
                }`}
                key={`${offer.retailer}-${offer.url}-${offerIndex}`}
              >
                <div className="offer-retailer-cell">
                  <span className="offer-retailer-name">
                    {offer.retailer || "Unknown retailer"}
                  </span>

                  {isCheapest && (
                    <span className="offer-best-badge">
                      LOWEST
                    </span>
                  )}
                </div>

                <strong className="offer-price">
                  {hasPrice ? money(price) : "Unavailable"}
                </strong>

                {offer.url ? (
                  <a
                    className="offer-link"
                    href={offer.url}
                    target="_blank"
                    rel="noopener noreferrer"
                  >
                    {offer.link_type === "retailer_direct"
                      ? "Visit store ↗"
                      : "View offer ↗"}
                  </a>
                ) : (
                  <span className="offer-link-unavailable">No link</span>
                )}
              </div>
            );
          })}
        </div>


        {/* Evidence-based discount checks for individual retailer offers */}
        {sortedOffers.map((offer, offerIndex) =>
          offer.discount_warning?.has_claim ? (
            <DiscountEvidence
              key={`discount-${offer.url}-${offerIndex}`}
              detector={offer.discount_warning}
              currency="INR"
            />
          ) : null
        )}


        {cheapest?.url && (
          <a
            className="btn-primary lowest-link"
            href={cheapest.url}
            target="_blank"
            rel="noopener noreferrer"
          >
            Open lowest listed offer ↗
          </a>
        )}

        <p className="verification-note">
          Prices are reported by the search provider and may change.
          Check the exact model, specifications, seller, delivery fees
          and final checkout price before buying.
        </p>

        {offers.some((offer) =>
          ["claimed_discount_unverified", "suspicious_claim"].includes(
            offer.discount_warning?.status
          )
        ) && (
          <p className="discount-warning">
            ⚠ Some offers contain unverified discount claims.
            This is a warning, not proof of fraud.
          </p>
        )}

        <div className="product-actions">
          <button
            className="btn-primary"
            onClick={() => onTrack(product)}
            disabled={trackingId === product.title}
            type="button"
          >
            {trackingId === product.title
              ? "Tracking..."
              : "♡ Track product"}
          </button>
        </div>
      </div>
    </article>
  );
}

            const external = isExternalProduct(product);
            const price = getProductPrice(product);
            const key = external
              ? `${product.url || product.title}-${index}`
              : product.id;

            return (
              <article className="product-card" key={key}>
                <div className="product-image-wrap">
                  <img
                    className="product-image"
                    src={getProductImage(product)}
                    alt={product.title || "Product"}
                    onError={(event) => {
                      event.currentTarget.onerror = null;
                      event.currentTarget.src = PLACEHOLDER_IMAGE;
                    }}
                  />
                  <span className="price-status">
                    {getPriceLabel(product)}
                  </span>
                </div>

                <div className="product-info">
                  <span className="product-retailer">
                    {getRetailerName(product)}
                  </span>
                  <h3>{product.title}</h3>
                  <div className="product-price">
                    {money(price, product.currency || "INR")}
                  </div>

                  <p className="verification-note">
                    {external
                      ? price != null && Number(price) > 0
                        ? "Price reported by the search provider; confirm it on the retailer's website."
                        : "The provider did not return a usable price for this listing."
                      : "Catalog price; verify on the retailer's website."}
                  </p>

                  <div className="product-actions">
                    {external ? (
                      <button
                        className="btn-primary"
                        onClick={() => onTrack(product)}
                        type="button"
                      >
                        ♡ Track product
                      </button>
                    ) : (
                      <button
                        className="btn-primary"
                        onClick={() => onTrack(product)}
                        disabled={trackingId === product.id}
                        type="button"
                      >
                        {trackingId === product.id
                          ? "Tracking..."
                          : "♡ Track product"}
                      </button>
                    )}

                    {product.url && (
                      <a
                        href={product.url}
                        target="_blank"
                        rel="noreferrer"
                        className="btn-icon"
                        aria-label="Open retailer listing"
                      >
                        ↗
                      </a>
                    )}
                  </div>
                </div>
              </article>
            );
          })}
        </div>
      )}
    </div>
  );
}


function WatchlistPage({
  items,
  loading,
  onRemove,
  onNavigate,
  token,
  onNotify,
  onError,
}) {
  // State for the existing price-history panel.
  const [historyFor, setHistoryFor] = useState(null);
  const [historyData, setHistoryData] = useState(null);
  const [historyLoading, setHistoryLoading] = useState(false);

  // State for the target-price inputs.
  const [targetValues, setTargetValues] = useState({});

  // State for the new manual refresh button.
  const [refreshing, setRefreshing] = useState(false);
  const [refreshResult, setRefreshResult] = useState(null);

  // Refresh all products tracked by the signed-in user.
  const refreshPrices = async () => {
    // Prevent repeated clicks while a refresh is running.
    if (refreshing) return;

    setRefreshing(true);
    setRefreshResult(null);
    onError?.("");
    onNotify?.("");

    try {
      const response = await axios.post(
        `${API}/api/products/watchlist/refresh`,
        {},
        {
          headers: {
            Authorization: `Bearer ${token}`,
          },
          // Allow time for several retailer pages to be checked.
          timeout: 180000,
        }
      );

      const result = response.data;
      setRefreshResult(result);

      // Tell the user what the backend actually reported.
      if (result.error) {
        onError?.(result.error);
      } else {
        onNotify?.(
          `Refresh finished: ${result.checked} products checked, ` +
          `${result.skipped} skipped, ${result.alerts_sent} email alerts sent.`
        );
      }
    } catch (error) {
      // The backend returns 409 when another refresh is already running.
      const statusCode = error.response?.status;
      const detail = error.response?.data?.detail;

      if (statusCode === 409) {
        onError?.(
          typeof detail === "string"
            ? detail
            : "A refresh is already running. Please try again shortly."
        );
      } else if (statusCode === 401) {
        onError?.("Your login may have expired. Please log in again.");
      } else {
        onError?.(
          typeof detail === "string"
            ? detail
            : "Could not refresh prices. Check your connection and backend logs."
        );
      }
    } finally {
      // Always re-enable the button after the request finishes.
      setRefreshing(false);
    }
  };

  // Load history for the selected tracked product.
  const openHistory = async (item) => {
    setHistoryFor(item.subscription_id);
    setHistoryData(null);
    setHistoryLoading(true);

    try {
      const response = await axios.get(
        `${API}/api/products/watchlist/${item.subscription_id}/history`,
        {
          headers: {
            Authorization: `Bearer ${token}`,
          },
        }
      );

      setHistoryData(response.data);
    } catch {
      setHistoryData({
        error: "Could not load price history yet.",
      });
    } finally {
      setHistoryLoading(false);
    }
  };

  // Save or clear the target price for an individual subscription.
  const saveTarget = async (item) => {
    const raw = targetValues[item.subscription_id];

    const targetPrice =
      raw === undefined
        ? item.target_price
        : raw.trim() === ""
          ? null
          : Number(raw);

    // Reject invalid target prices before sending the request.
    if (
      targetPrice !== null &&
      (!Number.isFinite(targetPrice) || targetPrice <= 0)
    ) {
      onError?.("Please enter a valid target price greater than ₹0.");
      return;
    }

    onError?.("");
    onNotify?.("");

    try {
      const response = await axios.patch(
        `${API}/api/products/watchlist/${item.subscription_id}/target`,
        { target_price: targetPrice },
        {
          headers: {
            Authorization: `Bearer ${token}`,
          },
        }
      );

      // Keep the input synchronized with the saved value.
      setTargetValues((old) => ({
        ...old,
        [item.subscription_id]:
          targetPrice === null ? "" : String(targetPrice),
      }));

      onNotify?.(
        response.data?.message || "Target price updated successfully."
      );
    } catch (error) {
      const message =
        error.response?.data?.detail || "Could not save target price.";

      onError?.(
        typeof message === "string"
          ? message
          : "Could not save target price."
      );
    }
  };

  return (
    <div className="page-stack">
      <section className="page-intro">
        <span className="eyebrow">YOUR PERSONAL COLLECTION</span>

        <h2>
          Things worth <span>watching.</span>
        </h2>

        <p>Your tracked products, in one place.</p>
      </section>

      {/* Summary and manual refresh button. */}
      <div className="watchlist-summary">
        <div>
          <span className="eyebrow">SAVED PRODUCTS</span>
          <strong>{items.length.toString().padStart(2, "0")}</strong>
        </div>

        <span className="watchlist-heart">♡</span>

        <button
          className="btn-primary"
          type="button"
          onClick={refreshPrices}
          disabled={refreshing || loading || items.length === 0}
        >
          {refreshing ? (
            <>
              <span className="loader" /> Refreshing...
            </>
          ) : (
            <>↻ Refresh Prices</>
          )}
        </button>
      </div>

      {/* Explain what the last refresh actually did. */}
      {refreshResult && (
        <div className="history-card" role="status">
          <h4>Latest refresh results</h4>

          <div className="history-stats">
            <span>
              Products checked
              <strong>{refreshResult.checked ?? 0}</strong>
            </span>

            <span>
              History points saved
              <strong>{refreshResult.history_points ?? 0}</strong>
            </span>

            <span>
              Products skipped
              <strong>{refreshResult.skipped ?? 0}</strong>
            </span>

            <span>
              Email alerts sent
              <strong>{refreshResult.alerts_sent ?? 0}</strong>
            </span>
          </div>

          {refreshResult.error && (
            <p>{refreshResult.error}</p>
          )}

          {(refreshResult.skipped ?? 0) > 0 && (
            <p>
              Some retailer pages could not provide a reliable price.
              Their prices were not fabricated or replaced with guesses.
            </p>
          )}

          {refreshResult.checked > 0 &&
            refreshResult.history_points === 0 && (
              <p>
                Prices were retrieved, but no new history points were due
                under the current history-recording rules.
              </p>
            )}
        </div>
      )}

      {loading ? (
        <div className="empty-state">
          <span className="loader" /> Loading your watchlist...
        </div>
      ) : items.length === 0 ? (
        <div className="empty-state">
          <div className="empty-icon">♡</div>
          <h3>Your watchlist is waiting</h3>
          <p>
            Search the saved catalog and track products you want to follow.
          </p>

          <button
            className="btn-primary"
            onClick={() => onNavigate("search")}
            type="button"
          >
            Find products <span>↗</span>
          </button>
        </div>
      ) : (
        <div className="watchlist-list">
          {items.map((item) => (
            <article
              className="watchlist-item"
              key={item.subscription_id}
            >
              <img
                src={getProductImage(item)}
                alt={item.title}
                onError={(event) => {
                  event.currentTarget.onerror = null;
                  event.currentTarget.src = PLACEHOLDER_IMAGE;
                }}
              />

              <div className="watchlist-product">
                <span className="product-retailer">
                  {getRetailerName(item)}
                </span>

                <h3>{item.title}</h3>

                <span className="verification-note">
                  Catalog price; verify before purchase.
                </span>
              </div>

              <div className="watchlist-price">
                <small>CURRENT LISTED PRICE</small>
                <strong>{productMoney(item.current_price)}</strong>

                <small>
                  Target:{" "}
                  {item.target_price == null
                    ? "Not set"
                    : productMoney(item.target_price)}
                </small>
              </div>

              <div className="watchlist-actions">
                {item.url && (
                  <a
                    className="btn-secondary"
                    href={item.url}
                    target="_blank"
                    rel="noreferrer"
                  >
                    View ↗
                  </a>
                )}

                <button
                  className="btn-secondary"
                  onClick={() => openHistory(item)}
                  type="button"
                >
                  Price history
                </button>

                <button
                  className="btn-danger"
                  onClick={() => onRemove(item.subscription_id)}
                  type="button"
                >
                  Remove
                </button>
              </div>

              {/* Target-price alert settings. */}
              <div className="target-price-editor">
                <label htmlFor={`target-${item.subscription_id}`}>
                  Alert me at (₹)
                </label>

                <input
                  id={`target-${item.subscription_id}`}
                  type="number"
                  min="1"
                  placeholder={item.target_price ?? "Target price"}
                  value={targetValues[item.subscription_id] ?? ""}
                  onChange={(event) =>
                    setTargetValues((old) => ({
                      ...old,
                      [item.subscription_id]: event.target.value,
                    }))
                  }
                />

                <button
                  className="btn-secondary"
                  type="button"
                  onClick={() => saveTarget(item)}
                >
                  Save target
                </button>
              </div>

              {/* Price history, statistics and forecast. */}
              {historyFor === item.subscription_id && (
                <div className="history-card">
                  <div className="history-card-heading">
                    <h4>Price history · {item.title}</h4>

                    <button
                      type="button"
                      className="btn-icon"
                      onClick={() => setHistoryFor(null)}
                    >
                      ×
                    </button>
                  </div>

                  {historyLoading ? (
                    <p>Loading history…</p>
                  ) : historyData?.error ? (
                    <p>{historyData.error}</p>
                  ) : historyData ? (
                    <>
                      <div className="history-stats">
                        <span>
                          Average drop
                          <strong>
                            {money(historyData.average_drop ?? 0)}
                          </strong>
                        </span>

                        <span>
                          Average rise
                          <strong>
                            {money(historyData.average_rise ?? 0)}
                          </strong>
                        </span>

                        <span>
                          Net change
                          <strong>
                            {money(historyData.net_change ?? 0)}
                          </strong>
                        </span>
                      </div>

                      <PriceHistoryGraph
                        history={historyData.history || []}
                      />

                      <div className="forecast-card">
                        <h4>Expected price movement · 30 days</h4>

                        {historyData.forecast?.status !== "available" ? (
                          <p>
                            Not enough historical data to make a useful
                            forecast yet. Keep collecting prices over time.
                          </p>
                        ) : (
                          <>
                            <p>{historyData.forecast.message}</p>

                            <div className="forecast-stats">
                              <span>
                                Estimated price
                                <strong>
                                  {money(
                                    historyData.forecast.estimated_price
                                  )}
                                </strong>
                              </span>

                              <span>
                                Possible trend range
                                <strong>
                                  {money(
                                    historyData.forecast.estimated_low
                                  )}
                                  {" – "}
                                  {money(
                                    historyData.forecast.estimated_high
                                  )}
                                </strong>
                              </span>
                            </div>

                            <p className="forecast-disclaimer">
                              This is a basic trend estimate, not a
                              guarantee or a statistically validated
                              confidence interval.
                            </p>
                          </>
                        )}
                      </div>
                    </>
                  ) : null}
                </div>
              )}
            </article>
          ))}
        </div>
      )}
    </div>
  );
}



function PriceHistoryGraph({ history = [] }) {
  const now = new Date();
  const rangeEnd = now;
  const rangeStart = new Date(now);
  rangeStart.setDate(rangeStart.getDate() - 89);

  const points = history
    .map((point) => ({
      price: Number(point.price),
      date: point.recorded_at ? new Date(point.recorded_at) : null,
    }))
    .filter(
      (point) =>
        Number.isFinite(point.price) &&
        point.price > 0 &&
        point.date &&
        !Number.isNaN(point.date.getTime()) &&
        point.date >= rangeStart &&
        point.date <= rangeEnd
    )
    .sort((a, b) => a.date - b.date);

  const width = 700;
  const height = 280;
  const padding = { top: 30, right: 24, bottom: 55, left: 75 };

  const chartWidth = width - padding.left - padding.right;
  const chartHeight = height - padding.top - padding.bottom;

  const prices = points.map((p) => p.price);
  const actualMin = prices.length ? Math.min(...prices) : 0;
  const actualMax = prices.length ? Math.max(...prices) : 1;
  const priceRange = actualMax - actualMin;

  const displayMin =
    prices.length === 0
      ? 0
      : Math.max(0, actualMin - (priceRange || actualMin * 0.1) * 0.12);

  const displayMax =
    prices.length === 0
      ? 1
      : actualMax + (priceRange || actualMax * 0.1) * 0.12;

  const displayRange = displayMax - displayMin || 1;

  // Position each observation by its real date within the 90-day window.
  const coordinates = points.map((point) => {
    const elapsed =
      point.date.getTime() - rangeStart.getTime();
    const total =
      rangeEnd.getTime() - rangeStart.getTime();

    return {
      ...point,
      x: padding.left + (elapsed / total) * chartWidth,
      y:
        padding.top +
        ((displayMax - point.price) / displayRange) * chartHeight,
    };
  });

  const linePoints = coordinates
    .map((point) => `${point.x},${point.y}`)
    .join(" ");

  const formatDate = (date) =>
    date.toLocaleDateString("en-IN", {
      day: "2-digit",
      month: "short",
    });

  const formatAxisDate = (date) =>
    date.toLocaleDateString("en-IN", {
      day: "2-digit",
      month: "short",
      year: "2-digit",
    });

  const moneyLabel = (value) =>
    `₹${Math.round(value).toLocaleString("en-IN")}`;

  const first = points[0];
  const latest = points[points.length - 1];
  const change = first && latest ? latest.price - first.price : 0;
  const changePercent =
    first && first.price > 0 ? (change / first.price) * 100 : 0;

  return (
    <div className="price-history-chart">
      <div className="history-chart-summary">
        <div>
          <span className="history-chart-label">
            LATEST RECORDED PRICE
          </span>
          <strong>{latest ? money(latest.price) : "—"}</strong>
          <small>{latest ? formatAxisDate(latest.date) : "No data yet"}</small>
        </div>

        {points.length > 1 && (
          <div className="history-chart-change">
            <span className="history-chart-label">
              CHANGE SINCE FIRST RECORD
            </span>
            <strong
              className={
                change < 0 ? "price-down" : change > 0 ? "price-up" : ""
              }
            >
              {change > 0 ? "+" : ""}
              {moneyLabel(change)}
            </strong>
            <small>
              {changePercent > 0 ? "+" : ""}
              {changePercent.toFixed(2)}%
            </small>
          </div>
        )}
      </div>

      <div className="history-chart-svg-wrap">
        <svg
          viewBox={`0 0 ${width} ${height}`}
          role="img"
          aria-label={`90-day price history with ${points.length} recorded observations`}
        >
          {[0, 0.5, 1].map((fraction) => {
            const y = padding.top + fraction * chartHeight;
            const price = displayMax - fraction * displayRange;

            return (
              <g key={fraction}>
                <line
                  x1={padding.left}
                  y1={y}
                  x2={width - padding.right}
                  y2={y}
                  className="history-grid-line"
                />
                <text
                  x={padding.left - 10}
                  y={y + 4}
                  textAnchor="end"
                  className="history-axis-label"
                >
                  {moneyLabel(price)}
                </text>
              </g>
            );
          })}

          {/* Fixed date axis: always shows the complete 90-day window. */}
          {[0, 30, 60, 89].map((day) => {
            const date = new Date(rangeStart);
            date.setDate(date.getDate() + day);
            const x = padding.left + (day / 89) * chartWidth;

            return (
              <g key={day}>
                <line
                  x1={x}
                  y1={padding.top}
                  x2={x}
                  y2={padding.top + chartHeight}
                  className="history-grid-line"
                  opacity="0.45"
                />
                <text
                  x={x}
                  y={height - 20}
                  textAnchor={
                    day === 0 ? "start" : day === 89 ? "end" : "middle"
                  }
                  className="history-axis-label"
                >
                  {formatDate(date)}
                </text>
              </g>
            );
          })}

          {coordinates.length > 1 && (
            <polyline
              points={linePoints}
              fill="none"
              className="history-line"
              strokeWidth="3"
              strokeLinejoin="round"
              strokeLinecap="round"
            />
          )}

          {coordinates.map((point, index) => (
            <circle
              key={`${point.date.toISOString()}-${index}`}
              cx={point.x}
              cy={point.y}
              r="5"
              className="history-point"
            >
              <title>
                {formatAxisDate(point.date)} — {moneyLabel(point.price)}
              </title>
            </circle>
          ))}
        </svg>
      </div>

      <div className="chart-range">
        <span>
          Lowest: {points.length ? moneyLabel(actualMin) : "—"}
        </span>
        <span>
          Highest: {points.length ? moneyLabel(actualMax) : "—"}
        </span>
        <span>{points.length} recorded observations · Last 90 days</span>
      </div>

      {points.length === 0 && (
        <p className="history-chart-note">
          No recorded prices in this 90-day period yet. Prices will appear
          as the tracker collects genuine observations.
        </p>
      )}

      {points.length === 1 && (
        <p className="history-chart-note">
          Only one price observation is available. The full 90-day timeline
          is shown; the graph will develop as more prices are recorded.
        </p>
      )}

      {points.length > 1 && actualMin === actualMax && (
        <p className="history-chart-note">
          All recorded prices are currently the same. The line is flat
          because no price change has been recorded.
        </p>
      )}
    </div>
  );
}




function ProfilePage({
  user,
  email,
  setEmail,
  password,
  setPassword,
  authMode,
  setAuthMode,
  loading,
  onSubmit,
  onLogout,
  onNavigate,
}) {
  return (
    <div className="page-stack">
      <section className="page-intro">
        <span className="eyebrow">YOUR ACCOUNT, YOUR RULES</span>
        <h2>
          Make it <span>personal.</span>
        </h2>
        <p>Manage your account and make shopping research yours.</p>
      </section>

      <div className="account-layout">
        <section className="account-intro">
          <div className="account-graphic">
            <div className="account-graphic-ring" />
            <span>✳</span>
          </div>
          <span className="eyebrow">PRICEPULSE MEMBER SPACE</span>
          <h2>
            Good decisions start with <span>good information.</span>
          </h2>
          <p>
            Keep your tracked products together and return to your shopping
            research whenever you need it.
          </p>
          <div className="account-benefit">
            <span>✓</span> Personal tracked-product list
          </div>
          <div className="account-benefit">
            <span>✓</span> Saved account session
          </div>
          <div className="account-benefit">
            <span>✓</span> Dark and light appearance
          </div>
        </section>

        <section className="auth-card">
          {user ? (
            <div className="signed-in">
              <div className="signed-avatar">
                {user.charAt(0).toUpperCase()}
              </div>
              <span className="eyebrow">ACCOUNT CONNECTED</span>
              <h2>You're in.</h2>
              <p className="signed-email">{user}</p>
              <div className="account-detail-row">
                <span>Account status</span>
                <strong>
                  <span className="status-dot online" /> Logged in
                </strong>
              </div>
              <button
                className="btn-primary full-width"
                onClick={() => onNavigate("watchlist")}
                type="button"
              >
                View tracked products ↗
              </button>
              <button
                className="btn-danger full-width"
                onClick={onLogout}
                type="button"
              >
                Log out
              </button>
            </div>
          ) : (
            <>
              <span className="eyebrow">WELCOME TO YOUR SPACE</span>
              <h2>
                {authMode === "login"
                  ? "Welcome back."
                  : "Let's get started."}
              </h2>
              <p className="auth-description">
                {authMode === "login"
                  ? "Log in to pick up where you left off."
                  : "Create an account to start your own watchlist."}
              </p>

              <div className="auth-tabs">
                <button
                  type="button"
                  className={authMode === "login" ? "selected" : ""}
                  onClick={() => setAuthMode("login")}
                >
                  Log in
                </button>
                <button
                  type="button"
                  className={authMode === "register" ? "selected" : ""}
                  onClick={() => setAuthMode("register")}
                >
                  Create account
                </button>
              </div>

              <form className="auth-form" onSubmit={onSubmit}>
                <label htmlFor="account-email">EMAIL ADDRESS</label>
                <input
                  id="account-email"
                  type="email"
                  autoComplete="email"
                  placeholder="you@example.com"
                  value={email}
                  onChange={(event) => setEmail(event.target.value)}
                  required
                />

                <label htmlFor="account-password">PASSWORD</label>
                <input
                  id="account-password"
                  type="password"
                  autoComplete={
                    authMode === "login"
                      ? "current-password"
                      : "new-password"
                  }
                  placeholder="Enter your password"
                  value={password}
                  onChange={(event) => setPassword(event.target.value)}
                  minLength={6}
                  required
                />

                <button
                  className="btn-primary full-width"
                  disabled={loading}
                  type="submit"
                >
                  {loading
                    ? "Please wait..."
                    : authMode === "login"
                      ? "Log in to account ↗"
                      : "Create my account ↗"}
                </button>
              </form>

              <p className="auth-footnote">
                Never share your password or access token.
              </p>
            </>
          )}
        </section>
      </div>
    </div>
  );
}


function BudgetBasket({ watchlist }) {
  const [budget, setBudget] = useState(
    () => localStorage.getItem("budget_basket_amount") || "20000"
  );

  const [selectedIds, setSelectedIds] = useState([]);

  useEffect(() => {
    localStorage.setItem("budget_basket_amount", budget);
  }, [budget]);

  const toggleProduct = (id) => {
    setSelectedIds((current) =>
      current.includes(id)
        ? current.filter((selectedId) => selectedId !== id)
        : [...current, id]
    );
  };

  const selectedProducts = watchlist.filter((item) =>
    selectedIds.includes(item.subscription_id)
  );

  const pricedProducts = selectedProducts.filter((item) => {
    const price = Number(item.current_price);
    return (
      item.current_price !== null &&
      item.current_price !== undefined &&
      item.current_price !== "" &&
      Number.isFinite(price) &&
      price > 0
    );
  });

  const unavailableCount = selectedProducts.length - pricedProducts.length;

  const total = pricedProducts.reduce(
    (sum, item) => sum + Number(item.current_price),
    0
  );

  const budgetAmount = Number(budget);
  const validBudget =
    budget.trim() !== "" &&
    Number.isFinite(budgetAmount) &&
    budgetAmount >= 0;

  const difference = validBudget ? budgetAmount - total : 0;

  return (
    <div className="page-stack">
      <section className="page-intro">
        <span className="eyebrow">PLAN YOUR PURCHASES</span>
        <h2>
          Your budget. <span>Your basket.</span>
        </h2>
        <p>
          Select tracked products to estimate your spending before you buy.
        </p>
      </section>

      <section className="stat-card budget-card">
        <label htmlFor="basket-budget" className="stat-label">
          YOUR TOTAL BUDGET (₹)
        </label>

        <input
          id="basket-budget"
          className="budget-input"
          type="number"
          min="0"
          step="0.01"
          value={budget}
          onChange={(event) => setBudget(event.target.value)}
          placeholder="Enter your budget"
        />

        <p className="stat-detail">
          Your budget is saved in this browser.
        </p>
      </section>

      <section className="section-block">
        <div className="section-heading">
          <div>
            <span className="eyebrow">YOUR TRACKED PRODUCTS</span>
            <h2>Select items for your basket</h2>
            <p>
              Select or deselect products to update the calculation.
            </p>
          </div>
        </div>

        {!watchlist.length ? (
          <div className="empty-state">
            <div className="empty-icon">▣</div>
            <h3>No tracked products yet</h3>
            <p>
              Track some products first, then return here to plan your budget.
            </p>
          </div>
        ) : (
          <div className="basket-list">
            {watchlist.map((item) => {
              const rawPrice = item.current_price;
              const price = Number(rawPrice);

              const hasPrice =
                rawPrice !== null &&
                rawPrice !== undefined &&
                rawPrice !== "" &&
                Number.isFinite(price) &&
                price > 0;

              return (
                <label
                  className={`basket-item ${
                    selectedIds.includes(item.subscription_id) ? "selected" : ""
                  }`}
                  key={item.subscription_id}
                >
                  <input
                    type="checkbox"
                    aria-label={`Select ${item.title || "product"}`}
                    checked={selectedIds.includes(item.subscription_id)}
                    onChange={() => toggleProduct(item.subscription_id)}
                  />

                  <img
                    src={getProductImage(item)}
                    alt={item.title || "Product"}
                    onError={(event) => {
                      event.currentTarget.onerror = null;
                      event.currentTarget.src = PLACEHOLDER_IMAGE;
                    }}
                  />

                  <div className="basket-product">
                    <span className="product-retailer">
                      {getRetailerName(item)}
                    </span>
                    <h3>{item.title || "Untitled product"}</h3>
                  </div>

                  <div className="basket-price">
                    <small>LISTED PRICE</small>
                    <strong>
                      {hasPrice ? productMoney(price) : "Price unavailable"}
                    </strong>
                  </div>
                </label>
              );
            })}
          </div>
        )}
      </section>

      <section className="stats-grid stats-grid-3">
        <div className="stat-card">
          <span className="stat-label">SELECTED ITEMS</span>
          <strong className="stat-value">{selectedProducts.length}</strong>
          <span className="stat-detail">Products in your basket</span>
        </div>

        <div className="stat-card">
          <span className="stat-label">ESTIMATED TOTAL</span>
          <strong className="stat-value">
            {productMoney(total)}
          </strong>
          <span className="stat-detail">
            Based on available listed prices
          </span>
        </div>

        <div className="stat-card">
          <span className="stat-label">
            {difference < 0 ? "OVER BUDGET" : "BUDGET REMAINING"}
          </span>
          <strong className="stat-value">
            {validBudget
              ? productMoney(Math.abs(difference))
              : "Enter a valid budget"}
          </strong>
          <span className="stat-detail">
            {validBudget
              ? difference < 0
                ? "Reduce your basket or increase your budget."
                : "Amount left after selected listed prices."
              : "Budget must be zero or greater."}
          </span>
        </div>
      </section>

      {unavailableCount > 0 && (
        <div className="notice notice-error">
          <span>!</span>
          <p>
            {unavailableCount} selected product
            {unavailableCount === 1 ? " has" : "s have"} no usable price.
            The total excludes {unavailableCount} item
            {unavailableCount === 1 ? "" : "s"} and may be incomplete.
          </p>
        </div>
      )}

      <p className="disclaimer">
        This is an estimate based on prices stored in your tracked list.
        Prices, delivery charges, discounts, and availability may change.
        Confirm the final price with the retailer before purchasing.
      </p>
    </div>
  );
}
