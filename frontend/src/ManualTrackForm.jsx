import { useState } from "react";
import axios from "axios";

const API =
  import.meta.env.VITE_API_URL || "http://localhost:8000";

export default function ManualTrackForm({ token, onSuccess }) {
  const [url, setUrl] = useState("");
  const [productName, setProductName] = useState("");
  const [targetPrice, setTargetPrice] = useState("");
  const [loading, setLoading] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  const handleSubmit = async (event) => {
    event.preventDefault();
    setMessage("");
    setError("");

    if (!token) {
      setError("Please log in before tracking a product.");
      return;
    }

    let productUrl;

    try {
      productUrl = new URL(url.trim());

      if (!["https:", "http:"].includes(productUrl.protocol)) {
        throw new Error("Invalid URL protocol.");
      }
    } catch {
      setError("Please enter a valid product URL starting with https://");
      return;
    }

    let target = null;

    if (targetPrice.trim() !== "") {
      target = Number(targetPrice);

      if (!Number.isFinite(target) || target <= 0) {
        setError("Target price must be greater than ₹0.");
        return;
      }
    }

    setLoading(true);
    const headers = { Authorization: `Bearer ${token}` };

    try {
      // 1. Retrieve product information from the retailer. If the store
      //    blocks the page, the name typed below is used instead.
      const body = { url: productUrl.href };
      const name = productName.trim();
      if (name) body.title = name;

      const ingestResponse = await axios.post(
        `${API}/api/products/ingest`,
        body,
        { timeout: 60000 }
      );

      const productId = ingestResponse.data?.product_id;

      if (!productId) {
        throw new Error("The server did not return a product ID.");
      }

      // 2. Add the product to the watchlist.
      const trackResponse = await axios.post(
        `${API}/api/products/${productId}/track`,
        {},
        { headers }
      );

      let subscriptionId = trackResponse.data?.subscription_id;

      // 3. Save the optional target price.
      if (target !== null) {
        if (!subscriptionId) {
          const response = await axios.get(
            `${API}/api/products/watchlist`,
            { headers }
          );

          const items = Array.isArray(response.data)
            ? response.data
            : response.data?.items || [];

          const item = items.find(
            (entry) =>
              String(entry.product_id ?? entry.product?.id) ===
              String(productId)
          );

          subscriptionId =
            item?.subscription_id ?? item?.id;
        }

        if (!subscriptionId) {
          throw new Error(
            "Product tracked, but the subscription ID was not returned. " +
            "The target price could not be saved."
          );
        }

        await axios.patch(
          `${API}/api/products/watchlist/${subscriptionId}/target`,
          { target_price: target },
          { headers }
        );
      }

      const verifiedPrice = ingestResponse.data?.price;

      if (ingestResponse.data?.used_manual_title) {
        setMessage(
          "Added using the name you entered. The store blocked the page, " +
          "so the price will appear after the next price refresh."
        );
      } else {
        setMessage(
          verifiedPrice == null
            ? "Product added to your watchlist. Its current price could not be verified yet."
            : `Product added successfully! Retrieved price: ₹${Number(verifiedPrice).toLocaleString("en-IN")}`
        );
      }

      setUrl("");
      setProductName("");
      setTargetPrice("");

      if (typeof onSuccess === "function") {
        await onSuccess();
      }
    } catch (err) {
      const detail =
        err.response?.data?.detail ||
        err.response?.data?.message ||
        err.message ||
        "Something went wrong.";

      setError(`Could not track this URL: ${detail}`);
    } finally {
      setLoading(false);
    }
  };

  return (
    <section className="manual-track-card">
      <div className="manual-track-heading">
        <h3>Track a Product by URL</h3>
        <p>
          Paste a direct product-page link from a supported retailer
          to start tracking its price.
        </p>
      </div>

      <form className="manual-track-form" onSubmit={handleSubmit}>
        <div className="manual-track-field manual-url-field">
          <label htmlFor="manual-product-url">Product URL</label>
          <input
            id="manual-product-url"
            type="url"
            value={url}
            onChange={(event) => setUrl(event.target.value)}
            placeholder="https://www.amazon.in/dp/..."
            autoComplete="url"
            required
          />
        </div>

        <div className="manual-track-field manual-name-field">
          <label htmlFor="manual-product-name">
            Product name <span>— used if the store blocks the page</span>
          </label>
          <input
            id="manual-product-name"
            type="text"
            value={productName}
            onChange={(event) => setProductName(event.target.value)}
            placeholder="e.g. Samsung Galaxy S25 FE 5G 128GB"
            maxLength={300}
          />
        </div>

        <div className="manual-track-field manual-target-field">
          <label htmlFor="manual-target-price">
            Target Price (₹) <span>— Optional</span>
          </label>
          <input
            id="manual-target-price"
            type="number"
            min="0.01"
            step="0.01"
            value={targetPrice}
            onChange={(event) => setTargetPrice(event.target.value)}
            placeholder="e.g. 75000"
            inputMode="decimal"
          />
        </div>

        <button
          className="manual-track-submit"
          type="submit"
          disabled={loading}
        >
          {loading ? "Checking Product..." : "Start Tracking"}
        </button>
      </form>

      {message && (
        <p className="manual-success" role="status">
          {message}
        </p>
      )}

      {error && (
        <p className="manual-error" role="alert">
          {error}
        </p>
      )}
    </section>
  );
}