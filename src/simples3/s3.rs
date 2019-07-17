// Originally from https://github.com/rust-lang/crates.io/blob/master/src/s3/lib.rs
//#![deny(warnings)]

#[allow(unused_imports, deprecated)]
use std::ascii::AsciiExt;
use std::fmt;

use base64;
use crypto::digest::Digest;
use crypto::hmac::Hmac;
use crypto::mac::Mac;
use crypto::sha1::Sha1;
use futures::{Future, Stream};
use hyper::{self, header::{self, HeaderName, HeaderValue}};
use hyper::{Body, Method, Request};
use hyper::client::{Client, HttpConnector};
use hyper_tls::HttpsConnector;
use simples3::credential::*;
use time;
use tokio_core::reactor::Handle;

use errors::*;

#[derive(Debug, Copy, Clone)]
#[allow(dead_code)]
/// Whether or not to use SSL.
pub enum Ssl {
    /// Use SSL.
    Yes,
    /// Do not use SSL.
    No,
}

fn base_url(endpoint: &str, ssl: Ssl) -> String {
    format!("{}://{}/",
            match ssl {
                Ssl::Yes => "https",
                Ssl::No => "http",
            },
            endpoint)
}

fn hmac<D: Digest>(d: D, key: &[u8], data: &[u8]) -> Vec<u8> {
    let mut hmac = Hmac::new(d, key);
    hmac.input(data);
    hmac.result().code().iter().map(|b| *b).collect::<Vec<u8>>()
}

fn signature(string_to_sign: &str, signing_key: &str) -> String {
    let s = hmac(Sha1::new(), signing_key.as_bytes(), string_to_sign.as_bytes());
    base64::encode_config::<Vec<u8>>(&s, base64::STANDARD)
}

/// An S3 bucket.
pub struct Bucket {
    name: String,
    base_url: String,
    client: Client<HttpsConnector<HttpConnector>>,
}

impl fmt::Display for Bucket {
    fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result {
        write!(f, "Bucket(name={}, base_url={})", self.name, self.base_url)
    }
}

impl Bucket {
    pub fn new(name: &str, endpoint: &str, ssl: Ssl, handle: &Handle)
        -> Result<Bucket>
    {
        let base_url = base_url(&endpoint, ssl);
        Ok(Bucket {
            name: name.to_owned(),
            base_url: base_url,
            client: Client::builder()
                        .build(HttpsConnector::new(1)?),
        })
    }

    pub fn get(&self, key: &str) -> SFuture<Vec<u8>> {
        let url = format!("{}{}", self.base_url, key);
        debug!("GET {}", url);
        let url2 = url.clone();
        Box::new(self.client.get(url.parse().unwrap()).chain_err(move || {
            format!("failed GET: {}", url)
        }).and_then(|res| {
            if res.status().is_success() {
                let content_length = res.headers().get(header::CONTENT_LENGTH)
                    .map(|len| len.to_str().unwrap().parse::<usize>().unwrap());
                Ok((res.into_body(), content_length))
            } else {
                Err(ErrorKind::BadHTTPStatus(res.status().clone()).into())
            }
        }).and_then(|(body, content_length)| {
            body.fold(Vec::new(), |mut body, chunk| {
                body.extend_from_slice(&chunk);
                Ok::<_, hyper::Error>(body)
            }).chain_err(|| {
                "failed to read HTTP body"
            }).and_then(move |bytes| {
                if let Some(len) = content_length {
                    if len != bytes.len() {
                        bail!(format!("Bad HTTP body size read: {}, expected {}", bytes.len(), len));
                    } else {
                        info!("Read {} bytes from {}", bytes.len(), url2);
                    }
                }
                Ok(bytes)
            })
        }))
    }

    pub fn put(&self, key: &str, content: Vec<u8>, creds: &AwsCredentials)
               -> SFuture<()> {
        let url = format!("{}{}", self.base_url, key);
        debug!("PUT {}", url);
        let mut request = Request::put(url.parse::<String>().unwrap()).body(Body::from(content.clone())).unwrap();

        let content_type = "application/octet-stream";
        let date = time::now_utc().rfc822().to_string();
        let mut canonical_headers = String::new();
        let token = creds.token().as_ref().map(|s| s.as_str());
        // Keep the list of header values sorted!
        for (header, maybe_value) in vec![
            ("x-amz-security-token", token),
            ] {
            if let Some(ref value) = maybe_value {
                request.headers_mut()
                       .insert(HeaderName::from_static(header), HeaderValue::from_bytes(&value.as_bytes()).unwrap());
                canonical_headers.push_str(format!("{}:{}\n", header.to_ascii_lowercase(), value).as_ref());
            }
        }
        let auth = self.auth("PUT", &date, key, "", &canonical_headers, content_type, creds);
        request.headers_mut().insert(header::DATE, HeaderValue::from_bytes(&date.into_bytes()).unwrap());
        request.headers_mut().insert(header::CONTENT_TYPE, content_type.parse().unwrap());
        request.headers_mut().insert(header::CONTENT_LENGTH, HeaderValue::from_str(&(content.len().to_string())).unwrap());
        request.headers_mut().insert(header::CACHE_CONTROL, HeaderValue::from_static("max-age=1296000")); // Two weeks
        request.headers_mut().insert(header::AUTHORIZATION, HeaderValue::from_bytes(&auth.into_bytes()).unwrap());

        Box::new(self.client.request(request).then(|result| {
            match result {
                Ok(res) => {
                    if res.status().is_success() {
                        trace!("PUT succeeded");
                        Ok(())
                    } else {
                        trace!("PUT failed with HTTP status: {}", res.status());
                        Err(ErrorKind::BadHTTPStatus(res.status().clone()).into())
                    }
                }
                Err(e) => {
                    trace!("PUT failed with error: {:?}", e);
                    Err(e.into())
                }
            }
        }))
    }

    // http://docs.aws.amazon.com/AmazonS3/latest/dev/RESTAuthentication.html
    fn auth(&self, verb: &str, date: &str, path: &str,
            md5: &str, headers: &str, content_type: &str, creds: &AwsCredentials) -> String {
        let string = format!("{verb}\n{md5}\n{ty}\n{date}\n{headers}{resource}",
                             verb = verb,
                             md5 = md5,
                             ty = content_type,
                             date = date,
                             headers = headers,
                             resource = format!("/{}/{}", self.name, path));
        let signature = signature(&string, creds.aws_secret_access_key());
        format!("AWS {}:{}", creds.aws_access_key_id(), signature)
    }
}
