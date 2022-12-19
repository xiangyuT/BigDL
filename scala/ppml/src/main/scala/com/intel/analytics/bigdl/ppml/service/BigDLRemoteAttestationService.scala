package com.intel.analytics.bigdl.ppml.service

import java.util.concurrent.{LinkedBlockingQueue, TimeUnit}
import java.io.{File, InputStream}
import java.security.{KeyStore, SecureRandom}
import javax.net.ssl.{KeyManagerFactory, SSLContext, TrustManagerFactory}
import java.util.Base64

import akka.actor.ActorSystem
import akka.http.scaladsl.{ConnectionContext, Http, HttpsConnectionContext}
import akka.Done
import akka.http.scaladsl.server.Route
import akka.http.scaladsl.server.Directives._
import akka.http.scaladsl.model.StatusCodes
import akka.stream.ActorMaterializer
import akka.util.Timeout

import akka.http.scaladsl.marshallers.sprayjson.SprayJsonSupport._
import spray.json.DefaultJsonProtocol._

import scala.io.StdIn

import scala.concurrent.Future

import org.apache.logging.log4j.LogManager
import scopt.OptionParser

import com.intel.analytics.bigdl.ppml.attestation.verifier.SGXDCAPQuoteVerifierImpl

object BigDLRemoteAttestationService {

  implicit val system = ActorSystem("BigDLRemoteAttestationService")
  implicit val materializer = ActorMaterializer()
  implicit val executionContext = system.dispatcher
  implicit val timeout: Timeout = Timeout(100, TimeUnit.SECONDS)

  final case class Quote(quote: String)
  implicit val quoteFormat = jsonFormat1(Quote)

  final case class Result(result: Int)
  implicit val resultFormat = jsonFormat1(Result)

  val quoteVerifier = new SGXDCAPQuoteVerifierImpl()
        
  def main(args: Array[String]): Unit = {

    val logger = LogManager.getLogger(getClass)
    case class CmdParams(serviceURL: String = "localhost",
                          servicePort: String = "8184",
                          httpsKeyStoreToken: String = "token",
                          httpsKeyStorePath: String = "./key",
                          httpsEnabled: Boolean = false
                          )

    val cmdParser : OptionParser[CmdParams] = new OptionParser[CmdParams]("BigDL Remote Attestation Service") {
        opt[String]('u', "serviceURL")
          .text("Attestation Service URL")
          .action((x, c) => c.copy(serviceURL = x))
        opt[String]('p', "servicePort")
          .text("Attestation Service Port")
          .action((x, c) => c.copy(servicePort = x))
        opt[Boolean]('s', "httpsEnabled")
          .text("httpsEnabled")
          .action((x, c) => c.copy(httpsEnabled = x))        
        opt[String]('t', "httpsKeyStoreToken")
          .text("httpsKeyStoreToken")
          .action((x, c) => c.copy(httpsKeyStoreToken = x))
        opt[String]('h', "httpsKeyStorePath")
          .text("httpsKeyStorePath")
          .action((x, c) => c.copy(httpsKeyStorePath = x))
    }
    val params = cmdParser.parse(args, CmdParams()).get

    val route: Route =
        post {
          path("verifyQuote") {
            entity(as[Quote]) { quoteMsg =>
              println(quoteMsg)
              val verifyQuoteResult = quoteVerifier.verifyQuote(
                Base64.getDecoder().decode(quoteMsg.quote.getBytes))
              val res = new Result(verifyQuoteResult)
              complete(res)
            }
          }
        }
      
    val serviceURL = params.serviceURL
    val servicePort = params.servicePort
    val servicePortInt = servicePort.toInt
    if (params.httpsEnabled) {
      val serverContext = defineServerContext(params.httpsKeyStoreToken,
        params.httpsKeyStorePath)
      val bindingFuture = Http().bindAndHandle(route, serviceURL, servicePortInt, connectionContext=serverContext)
      println("Server online at https://%s:%s/\nPress RETURN to stop...".format(serviceURL, servicePort))
      StdIn.readLine()
      bindingFuture
        .flatMap(_.unbind())
        .onComplete(_ => system.terminate()) 
    } else {
      val bindingFuture = Http().bindAndHandle(route, serviceURL, servicePortInt)
      println("Server online at http://%s:%s/\nPress RETURN to stop...".format(serviceURL, servicePort))
      StdIn.readLine() 
      bindingFuture
        .flatMap(_.unbind()) 
        .onComplete(_ => system.terminate()) 
    }
  }

  def defineServerContext(httpsKeyStoreToken: String,
                          httpsKeyStorePath: String): ConnectionContext = {
    val token = httpsKeyStoreToken.toCharArray

    val keyStore = KeyStore.getInstance("PKCS12")
    val keystoreInputStream = new File(httpsKeyStorePath).toURI().toURL().openStream()

    keyStore.load(keystoreInputStream, token)

    val keyManagerFactory = KeyManagerFactory.getInstance("SunX509")
    keyManagerFactory.init(keyStore, token)

    val trustManagerFactory = TrustManagerFactory.getInstance("SunX509")
    trustManagerFactory.init(keyStore)

    val sslContext = SSLContext.getInstance("TLS")
    sslContext.init(keyManagerFactory.getKeyManagers,
      trustManagerFactory.getTrustManagers, new SecureRandom)

    ConnectionContext.https(sslContext)
  }
}

